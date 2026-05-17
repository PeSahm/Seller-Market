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
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID
from weakref import WeakSet

from sqlalchemy import delete, select

from app.db import AsyncSessionLocal
from app.models.runs import Run, StackRunLock
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
    """The actual ``docker exec`` payload for cache_warmup / run_trading.

    Every argument that comes from a DB row (``host`` and ``run_time`` are
    the obvious risks — they're user-set in the locust editor) is passed
    through ``shlex.quote`` before joining. A malicious value like
    ``"http://x ; rm -rf /"`` would otherwise break argument boundaries
    and run a shell command of the attacker's choosing — see the Phase 6
    PR review (#1).
    """
    container = f"{stack.compose_project}-bot"
    if job_name == "cache_warmup":
        return " ".join(
            shlex.quote(p)
            for p in ["docker", "exec", container, "python", "cache_warmup.py"]
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
            "docker", "exec", container,
            "locust", "-f", "locustfile_new.py", "--headless",
            "--users", str(users),
            "--spawn-rate", str(spawn_rate),
            "--run-time", run_time,
            "--host", host,
        ]
        if processes and processes != 1:
            parts.extend(["--processes", str(processes)])
        return " ".join(shlex.quote(str(p)) for p in parts)
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

    Concurrency is gated at TWO layers:

    1. A Postgres session-level advisory lock keyed on ``stack_id``
       serializes the whole "check existing lock → create Run → insert
       StackRunLock" sequence. Non-blocking (``pg_try_advisory_lock``):
       a concurrent caller is rejected immediately rather than queued.
    2. The ``stack_run_locks`` table holds the durable "what's running
       right now" record (with a TTL so a crashed mgmt process doesn't
       strand the lock forever).

    Without the advisory lock the SELECT + INSERT window was racy:
    two clicks landed simultaneously, both saw "no existing lock", both
    created Run rows, both inserted StackRunLock rows (one would later
    crash on the PK), and the rejection branch finalized a "killed" run
    polluting the history. With the gate, a rejected caller never
    creates a Run row at all — clean history.

    If the lock can't be acquired, raises :class:`StackRunLockBusyError`
    — handler surfaces as 409 (or browser redirect to in-flight run).
    """
    from sqlalchemy import text
    from app.db import hash_lock_key

    advisory_key = hash_lock_key("stack_run_acquire", str(stack_id))

    async with AsyncSessionLocal() as db:
        # Layer 1: session-level advisory gate — non-blocking. Either we
        # win the race instantly or another caller is already in flight
        # acquiring this stack's lock; either way the user sees the same
        # "busy" outcome.
        gate = await db.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": advisory_key}
        )
        if not gate.scalar():
            raise run_locks.StackRunLockBusyError(
                stack_id,
                holder="<concurrent acquire>",
                expires_at=datetime.now(timezone.utc),
            )
        try:
            # Layer 2: check the durable row — held by another in-flight
            # run? Then bail BEFORE creating any Run row.
            existing = await db.execute(
                select(StackRunLock).where(StackRunLock.stack_id == stack_id)
            )
            row = existing.scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if row is not None:
                if row.lease_expires_at > now:
                    raise run_locks.StackRunLockBusyError(
                        stack_id, row.holder, row.lease_expires_at
                    )
                # Stale lock — reclaim it. Logged inside acquire_lock too,
                # but we delete here so the upcoming acquire_lock INSERT
                # doesn't trip the PK constraint.
                await db.execute(
                    delete(StackRunLock).where(StackRunLock.stack_id == stack_id)
                )
                await db.commit()

            # Now we know the stack is free. Create the Run row (this
            # commits internally; advisory lock survives because it's
            # session-level, not transaction-level).
            run = await services_runs.start_run(
                db,
                stack_id=stack_id,
                agent_id=agent_id,
                job_name=job_name,
                trigger="manual",
                actor_id=actor_id,
            )
            # Insert the durable lock pointing at the Run we just made.
            await run_locks.acquire_lock(
                db,
                stack_id=stack_id,
                run_id=run.id,
                kind="cache" if job_name == "cache_warmup" else "trade",
                holder=_holder_for(actor_id),
            )
            await db.commit()
        finally:
            # Release the advisory lock no matter what — including the
            # busy-raise path, so the rejected caller doesn't strand it.
            try:
                await db.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": advisory_key}
                )
                await db.commit()
            except Exception:
                logger.exception(
                    "failed to release advisory lock key=%s for stack=%s",
                    advisory_key, stack_id,
                )

    # Fire-and-forget background task. We DON'T await it — the HTTP request
    # returns immediately and the browser opens a WebSocket for live log.
    asyncio.create_task(
        _run_executor_loop(run.id, stack_id, agent_id, job_name, actor_id),
        name=f"run-{run.id}",
    )
    return run


# How often the run-time heartbeat extends the stack lock's lease, in
# seconds. Default lease is 600s (10 min); we renew every 180s (3 min)
# so a single missed beat (timeout, transient DB error) still leaves
# ~4 minutes of cushion before the lease expires.
_LEASE_RENEW_INTERVAL_SECONDS = 180
_LEASE_RENEW_SECONDS = 600


async def _renew_lease_until_stopped(
    stack_id: UUID,
    run_id: UUID,
    stop_event: asyncio.Event,
) -> None:
    """Periodically extend the stack lock's lease until ``stop_event`` is set.

    Started as a side asyncio task by :func:`_run_executor_loop`. The
    streamer's hard ceiling is 30 minutes; without this heartbeat a run
    longer than the 10-minute default lease could see its lock reclaimed
    by stale-lock cleanup, allowing a concurrent click to slip through.

    Each extend opens its own short-lived session so a long-held one
    can't block other DB work. The extend is keyed on
    ``(stack_id, run_id)`` — if the lock has already been released or
    reclaimed (run_id mismatch), the UPDATE is a silent no-op rather
    than a cross-tenant lease bump.
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=_LEASE_RENEW_INTERVAL_SECONDS
            )
            return  # stop_event fired during the wait
        except asyncio.TimeoutError:
            pass
        try:
            async with AsyncSessionLocal() as db:
                await run_locks.extend_lease(
                    db,
                    stack_id=stack_id,
                    run_id=run_id,
                    seconds=_LEASE_RENEW_SECONDS,
                )
                await db.commit()
        except Exception:  # noqa: BLE001 — never let a beat-miss kill the run
            logger.exception(
                "lease renewal failed for stack=%s run=%s — will retry",
                stack_id, run_id,
            )


async def _run_executor_loop(
    run_id: UUID,
    stack_id: UUID,
    agent_id: UUID,
    job_name: str,
    actor_id: Optional[UUID],
) -> None:
    """The big workhorse — runs inside an asyncio task; failures are swallowed
    after being recorded into the Run row and the audit log.

    Spawns a side heartbeat task that periodically renews the stack lock's
    lease via ``run_locks.extend_lease``. The default lease is 10 minutes
    (see ``run_locks.DEFAULT_LEASE_SECONDS``) but the streamer's hard
    ceiling is 30 minutes — without renewal, a long locust run could see
    its own lock reclaimed by a stale-lock cleanup and a concurrent click
    would slip through. We extend every 3 minutes (1/3 of lease) so a
    single missed beat still keeps the lease alive.
    """
    captured = bytearray()
    final_status = "failed"
    final_exit_code: Optional[int] = None

    # Heartbeat task to renew the lease throughout the run. See PR #49 review #2.
    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _renew_lease_until_stopped(stack_id, run_id, heartbeat_stop),
        name=f"run-heartbeat-{run_id}",
    )

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
        # Stop the heartbeat first so a final extend_lease doesn't race
        # with release_lock below.
        heartbeat_stop.set()
        try:
            await asyncio.wait_for(heartbeat_task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            heartbeat_task.cancel()
        except Exception:  # noqa: BLE001
            logger.exception("heartbeat task ended with error run=%s", run_id)
        # Finalize + release lock no matter what. CRITICALLY: release the
        # lock even when finalize_run raises (e.g. log-archive I/O error
        # on a full disk) — otherwise the stack stays blocked until the
        # lease TTL expires (10 minutes by default). See PR #49 review #3.
        try:
            async with AsyncSessionLocal() as db:
                try:
                    await services_runs.finalize_run(
                        db,
                        run_id=run_id,
                        status=final_status,
                        exit_code=final_exit_code,
                        captured_log=bytes(captured),
                        actor_id=actor_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("finalize_run failed for run=%s", run_id)
                # Always attempt to release the lock and commit — even if
                # finalize_run blew up. The Run row may stay in
                # status='running' until manual cleanup, but at least the
                # stack isn't stuck.
                await run_locks.release_lock(db, stack_id=stack_id, run_id=run_id)
                await db.commit()
        except Exception:  # noqa: BLE001 — never let the cleanup raise
            logger.exception("release_lock failed for run=%s", run_id)
        # Sentinel publish so WS clients close cleanly.
        _publish(
            run_id, StreamingResult(exit_code=final_exit_code, captured=bytes(captured))
        )


__all__ = ["start_manual_run", "subscribe"]
