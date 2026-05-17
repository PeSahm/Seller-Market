"""Orchestrate a manual run: acquire lock, disable scheduler, exec, capture, restore.

Spawned by the POST /admin/stacks/{id}/run handler as a fire-and-forget
asyncio task — the HTTP request returns the new run_id immediately and
the browser switches to the WebSocket for live log.

We hold the stack_run_locks row for the full duration. WebSocket clients
fan out from the captured-output broadcast that run_executor publishes.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Optional
from uuid import UUID
from weakref import WeakSet

from app.db import AsyncSessionLocal
from app.models.runs import Run
from app.models.servers import Server
from app.services import locust_configs as services_locust
from app.services import run_locks
from app.services import runs as services_runs
from app.services import scheduler_snapshot
from app.services import stacks as services_stacks
from app.services.ssh.exceptions import SSHError
from app.services.ssh.streaming import LineEvent, StreamingResult, stream_remote_command

logger = logging.getLogger(__name__)

# In-memory fan-out from run_executor -> all WS subscribers for a given run.
# Keys are run_id (UUID); values are sets of asyncio.Queue. Each WS subscriber
# creates its own queue and registers it; executor publishes each LineEvent to
# every queue. WeakSet so disconnected clients are GC'd without book-keeping
# as soon as the WS handler's local reference to the queue is dropped.
_subscribers: dict[UUID, "WeakSet[asyncio.Queue]"] = {}


def _holder_for(actor_id: Optional[UUID]) -> str:
    return f"manual:{actor_id}" if actor_id else "manual:system"


def _build_command(job_name: str, stack, locust_cfg) -> str:
    """The actual ``docker exec`` payload for cache_warmup / run_trading."""
    container = f"{stack.compose_project}-bot"
    if job_name == "cache_warmup":
        return (
            f"docker exec {shlex.quote(container)} "
            f"python cache_warmup.py"
        )
    if job_name == "run_trading":
        if locust_cfg is None:
            # Render-time defaults — matches Phase 3's locust defaults.
            users, spawn_rate, run_time, host, processes = 10, 10, "120s", "https://abc.com", 1
        else:
            users = locust_cfg.users
            spawn_rate = locust_cfg.spawn_rate
            run_time = locust_cfg.run_time
            host = locust_cfg.host
            processes = locust_cfg.processes
        parts = [
            "docker", "exec", shlex.quote(container),
            "locust", "-f", "locustfile_new.py", "--headless",
            "--users", str(users),
            "--spawn-rate", str(spawn_rate),
            "--run-time", run_time,
            "--host", host,
        ]
        if processes and processes != 1:
            parts.extend(["--processes", str(processes)])
        return " ".join(parts)
    raise ValueError(f"unknown job_name: {job_name!r}")


def subscribe(run_id: UUID) -> asyncio.Queue:
    """Register a new WS listener; returns its queue.

    Unsubscribe is automatic via :class:`weakref.WeakSet` — once the WS
    handler's local reference to the returned queue is dropped, it is
    GC'd and removed from the set.
    """
    q: asyncio.Queue = asyncio.Queue()
    if run_id not in _subscribers:
        _subscribers[run_id] = WeakSet()
    _subscribers[run_id].add(q)
    return q


def _publish(run_id: UUID, item) -> None:
    subs = _subscribers.get(run_id)
    if not subs:
        return
    for q in list(subs):
        try:
            q.put_nowait(item)
        except Exception:  # noqa: BLE001
            pass


async def start_manual_run(
    *,
    stack_id: UUID,
    agent_id: UUID,
    job_name: str,
    actor_id: Optional[UUID],
) -> Run:
    """Synchronously create the run row + acquire the lock, then spawn the
    executor task. Returns the new Run immediately so the HTTP handler can
    redirect the browser to its detail page.

    If the lock can't be acquired, raises :class:`StackRunLockBusyError` —
    handler surfaces as 409.
    """
    async with AsyncSessionLocal() as db:
        run = await services_runs.start_run(
            db,
            stack_id=stack_id,
            agent_id=agent_id,
            job_name=job_name,
            trigger="manual",
            actor_id=actor_id,
        )
        try:
            await run_locks.acquire_lock(
                db,
                stack_id=stack_id,
                run_id=run.id,
                kind="cache" if job_name == "cache_warmup" else "trade",
                holder=_holder_for(actor_id),
            )
            await db.commit()
        except run_locks.StackRunLockBusyError:
            # The run row was created but we can't proceed — mark it killed
            # so the history reflects the rejection.
            await services_runs.finalize_run(
                db,
                run_id=run.id,
                status="killed",
                exit_code=None,
                captured_log=b"manual run rejected: another run already in flight\n",
                actor_id=actor_id,
            )
            raise

    # Fire-and-forget background task. We DON'T await it — the HTTP request
    # returns immediately and the browser opens a WebSocket for live log.
    asyncio.create_task(
        _run_executor_loop(run.id, stack_id, agent_id, job_name, actor_id),
        name=f"run-{run.id}",
    )
    return run


async def _run_executor_loop(
    run_id: UUID,
    stack_id: UUID,
    agent_id: UUID,
    job_name: str,
    actor_id: Optional[UUID],
) -> None:
    """The big workhorse — runs inside an asyncio task; failures are swallowed
    after being recorded into the Run row and the audit log.
    """
    captured = bytearray()
    final_status = "failed"
    final_exit_code: Optional[int] = None

    try:
        async with AsyncSessionLocal() as db:
            stack = await services_stacks.get_stack(db, stack_id)
            if stack is None:
                raise LookupError(f"stack {stack_id} vanished")
            server = await db.get(Server, stack.server_id)
            if server is None:
                raise LookupError(
                    f"server {stack.server_id} for stack {stack_id} vanished"
                )
            locust_cfg = await services_locust.get_locust_config(db, stack_id)
            command = _build_command(job_name, stack, locust_cfg)

            # Snapshot + disable scheduler for the duration of the run.
            async with scheduler_snapshot.disable_scheduler_for_stack(
                db,
                stack_id=stack_id,
                pusher=services_stacks.push_scheduler_config_for_stack,
                actor_id=actor_id,
            ):
                _publish(
                    run_id,
                    LineEvent("stdout", f"<run-executor> $ {command}".encode()),
                )
                async for ev in stream_remote_command(server, command, timeout=1800.0):
                    if isinstance(ev, LineEvent):
                        _publish(run_id, ev)
                        # captured is filled by the streamer itself, but
                        # we keep our own as a belt-and-braces fallback.
                        captured.extend(ev.data + b"\n")
                    elif isinstance(ev, StreamingResult):
                        final_exit_code = ev.exit_code
                        # Prefer the streamer's full capture (includes any
                        # bytes not split into lines).
                        if ev.captured:
                            captured = bytearray(ev.captured)
                        final_status = "success" if final_exit_code == 0 else "failed"
                        break
    except run_locks.StackRunLockBusyError:
        # Already handled at start_manual_run; shouldn't reach here.
        raise
    except LookupError as exc:
        _publish(run_id, LineEvent("stderr", f"<run-executor> {exc}".encode()))
        captured.extend(f"<run-executor> {exc}\n".encode())
        final_status = "failed"
    except SSHError as exc:
        _publish(run_id, LineEvent("stderr", f"<run-executor> ssh: {exc}".encode()))
        captured.extend(f"<run-executor> ssh: {exc}\n".encode())
        final_status = "failed"
    except Exception as exc:  # noqa: BLE001 — never let one run kill the worker
        logger.exception("run executor unexpected failure for run=%s", run_id)
        _publish(
            run_id, LineEvent("stderr", f"<run-executor> unexpected: {exc}".encode())
        )
        captured.extend(f"<run-executor> unexpected: {exc}\n".encode())
        final_status = "failed"
    finally:
        # Finalize + release lock no matter what.
        try:
            async with AsyncSessionLocal() as db:
                await services_runs.finalize_run(
                    db,
                    run_id=run_id,
                    status=final_status,
                    exit_code=final_exit_code,
                    captured_log=bytes(captured),
                    actor_id=actor_id,
                )
                await run_locks.release_lock(db, stack_id=stack_id, run_id=run_id)
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("run finalize failed for run=%s", run_id)
        # Sentinel publish so WS clients close cleanly.
        _publish(
            run_id, StreamingResult(exit_code=final_exit_code, captured=bytes(captured))
        )


__all__ = ["start_manual_run", "subscribe"]
