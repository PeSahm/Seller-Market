"""Periodic in-process worker that polls every agent stack's runtime state.

Every :data:`STACK_HEALTH_INTERVAL_SECONDS` we iterate over every
:class:`~app.models.stacks.AgentStack` row and ask the remote server "is this
compose project running?". The probe is ``docker compose ps --format json``;
the parser is tolerant of both JSON-array and JSON-Lines output that different
docker compose v2.x revisions emit.

Why a sibling worker instead of folding into ``app.workers.health``? Two
different cadences (servers tick every 60s, stacks every 30s) and two different
failure surfaces (an unreachable server flips itself to ``offline``; an
unreachable *stack* on a reachable server flips to ``down``). Keeping them
apart avoids cross-failure logic that's hard to read.

Resilience contract
-------------------
* Per-stack probes have a hard timeout cap (:data:`PER_STACK_TIMEOUT_SECONDS`)
  so a slow SSH session never stalls the loop.
* All exceptions from one stack's probe are swallowed and logged — they MUST
  NOT propagate out of :func:`_tick_once`.
* We never override the ``provisioning`` / ``deprovisioning`` transient
  statuses; those are driven by the service layer and our point-in-time
  ``ps`` would race the create / teardown sequence.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from sqlalchemy import select, update

from app.db import AsyncSessionLocal
from app.models.servers import Server
from app.models.stacks import AgentStack

logger = logging.getLogger(__name__)

STACK_HEALTH_INTERVAL_SECONDS = 30
# Short per-stack cap so one slow SSH handshake can't stall the whole loop.
PER_STACK_TIMEOUT_SECONDS = 15


def _parse_compose_status(stdout: str) -> str:
    """Return ``"up"`` if any container in ``stdout`` is running, else ``"down"``.

    Tolerant of both ``docker compose ps --format json`` shapes:

    * Older v2.x: a JSON array of objects.
    * Newer v2.x: JSON Lines (one object per line, no enclosing array).

    Garbage / partially-corrupt input falls through to ``"down"`` — a stack
    we can't introspect is no better than one we know is dead.
    """
    txt = stdout.strip()
    if not txt:
        return "down"

    candidates: list[dict] = []
    try:
        parsed = json.loads(txt)
        if isinstance(parsed, list):
            candidates = [c for c in parsed if isinstance(c, dict)]
        elif isinstance(parsed, dict):
            candidates = [parsed]
    except json.JSONDecodeError:
        # JSON-Lines fallback. We accept partial corruption: any line that
        # fails to parse is silently skipped, so "5 good objects + 1 garbage
        # line" still produces a sensible answer.
        for line in txt.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                candidates.append(obj)

    for c in candidates:
        # Defensive: some compose builds emit numeric / null / object values
        # under edge conditions. Coerce non-strings rather than crashing the
        # whole tick on a single weird container.
        raw_state = c.get("State") or c.get("state") or ""
        state = (
            raw_state.lower()
            if isinstance(raw_state, str)
            else str(raw_state).lower()
        )
        if state == "running":
            return "up"
    return "down"


async def _check_one(stack_id: UUID) -> None:
    """Probe one stack via ``docker compose ps`` and persist any status change.

    Never raises — all exceptions are logged and swallowed so a single bad
    stack can't stall the loop. SSH service modules are imported lazily so
    this worker can be imported (e.g. by :mod:`app.main`) even if those
    modules are mid-development by a parallel agent.
    """
    try:
        from app.services.ssh.commands import run_command
        from app.services.ssh.exceptions import SSHError

        async with AsyncSessionLocal() as db:
            stack = await db.get(AgentStack, stack_id)
            if stack is None:
                return
            server = await db.get(Server, stack.server_id)
            if server is None:
                return

            # ``--format json`` because the human-readable column layout
            # changes between docker versions; structured output is stable.
            cmd = (
                f"docker compose -p {stack.compose_project} "
                f"-f {stack.stack_dir}/docker-compose.yml ps --format json"
            )
            try:
                result = await asyncio.wait_for(
                    run_command(server, cmd, timeout=10.0),
                    timeout=PER_STACK_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, SSHError) as exc:
                logger.debug(
                    "stack_health.check stack=%s SSH failure: %s", stack_id, exc
                )
                new_status = "down"
            else:
                if not result.ok:
                    new_status = "down"
                else:
                    new_status = _parse_compose_status(result.stdout)

            # Transient transitions are owned by the service layer — don't
            # let a momentary ``ps`` mismatch race the create / teardown
            # flow. The SSH roundtrip above is slow (5-15s), and the row may
            # have been flipped to ``provisioning`` / ``deprovisioning`` by
            # the service layer while we were waiting. A read-then-write
            # against our stale ``stack`` snapshot would silently clobber
            # that transition. Use a conditional UPDATE so the database
            # itself enforces "only overwrite if not transitional and value
            # actually changes".
            stmt = (
                update(AgentStack)
                .where(AgentStack.id == stack_id)
                .where(
                    AgentStack.status.not_in(
                        ("provisioning", "deprovisioning")
                    )
                )
                .where(AgentStack.status != new_status)
                .values(status=new_status)
            )
            result = await db.execute(stmt)
            if result.rowcount:
                await db.commit()
    except Exception:  # noqa: BLE001 — never let one stack's failure kill the loop
        logger.exception(
            "stack_health.check stack=%s unexpected error", stack_id
        )


async def _tick_once() -> None:
    """One round: list every stack, dispatch a probe per stack, await all."""
    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(AgentStack.id))
        ids = [r[0] for r in rows.all()]
    if not ids:
        return
    await asyncio.gather(*[_check_one(sid) for sid in ids])


async def run_stack_health_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Long-running loop: tick every :data:`STACK_HEALTH_INTERVAL_SECONDS`.

    Returns once ``stop_event`` is set. Used both by the FastAPI startup hook
    in :mod:`app.main` and by unit tests (which pass their own event to drive
    a single tick and stop).
    """
    logger.info(
        "stack health worker started (interval=%ss)",
        STACK_HEALTH_INTERVAL_SECONDS,
    )
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            await _tick_once()
        except Exception:  # noqa: BLE001
            logger.exception("stack health worker tick failed")
        # Wait for either the interval elapsing or the stop_event firing,
        # whichever comes first. ``TimeoutError`` is the normal "interval
        # elapsed" path; not an error.
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=STACK_HEALTH_INTERVAL_SECONDS
            )
        except asyncio.TimeoutError:
            continue
    logger.info("stack health worker stopped")
