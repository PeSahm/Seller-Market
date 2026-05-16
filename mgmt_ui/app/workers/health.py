"""Periodic in-process health worker.

Every :data:`HEALTH_INTERVAL_SECONDS` we iterate over every non-deleted
``Server`` row and invoke :func:`app.services.servers.test_connection`. That
service is responsible for the side effects we care about — updating
``status``, ``last_seen_at``, and recording a fresh ``server_clock_skew_samples``
row. This module is purely the scheduling shell.

Why in-process? The ingestor in Phase 7 will run as its own container; for
Phase 2 we want a simple loop co-located with the FastAPI app so admins see
heartbeat updates immediately without standing up extra infrastructure.

Resilience contract
-------------------
* Per-server checks have a short timeout cap (:data:`PER_SERVER_TIMEOUT_SECONDS`)
  so a single slow / hanging server can never stall the loop.
* All exceptions from one server's check are swallowed and logged — they
  MUST NOT propagate out of :func:`_tick_once`.
* The loop honours a caller-supplied :class:`asyncio.Event` for prompt
  shutdown; if the event fires mid-sleep we return immediately instead of
  waiting for the next tick.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.servers import Server

logger = logging.getLogger(__name__)

HEALTH_INTERVAL_SECONDS = 60
# Short per-server cap so a single hung SSH handshake doesn't stall the loop.
PER_SERVER_TIMEOUT_SECONDS = 15


async def _check_one(server_id: UUID) -> None:
    """Health-check a single server. Never raises.

    The ``app.services.servers`` module is imported lazily so this worker
    package can be imported (e.g. by :mod:`app.main`) even while the
    services module is still being built out by a parallel agent. The
    runtime call will fail loudly via the broad ``except`` below if the
    module is missing, which is correct: at that point the deployment is
    broken and we want a log line per server, not a startup crash.
    """
    try:
        from app.services import servers as services_servers

        async with AsyncSessionLocal() as db:
            result = await asyncio.wait_for(
                services_servers.test_connection(db, server_id, actor_id=None),
                timeout=PER_SERVER_TIMEOUT_SECONDS,
            )
            logger.debug(
                "health.check server_id=%s ok=%s clock_skew=%s",
                server_id,
                getattr(result, "ok", None),
                getattr(result, "clock_skew_seconds", None),
            )
    except asyncio.TimeoutError:
        logger.warning("health.check server_id=%s timed out", server_id)
    except Exception:  # noqa: BLE001 — never let one server's failure kill the loop
        logger.exception("health.check server_id=%s unexpected error", server_id)


async def _tick_once() -> None:
    """One round: list non-deleted servers, dispatch a task per server, await all.

    The ``Server`` model has no ``deleted_at`` column yet (Phase 2 hard-deletes
    instead). When Phase 9 adds the column, the line below switches to::

        select(Server.id).where(Server.deleted_at.is_(None))

    For now the unfiltered select is correct because deleted rows physically
    don't exist.
    """
    async with AsyncSessionLocal() as db:
        # TODO(phase-9): add .where(Server.deleted_at.is_(None)) once soft-delete lands.
        rows = await db.execute(select(Server.id))
        ids = [r[0] for r in rows.all()]
    if not ids:
        return
    await asyncio.gather(*[_check_one(sid) for sid in ids])


async def run_health_worker(stop_event: asyncio.Event | None = None) -> None:
    """Long-running loop: tick every ``HEALTH_INTERVAL_SECONDS``.

    Returns once ``stop_event`` is set. Used both by the FastAPI startup hook
    in :mod:`app.main` and by unit tests (which pass their own event to drive
    a single tick and stop).
    """
    logger.info("health worker started (interval=%ss)", HEALTH_INTERVAL_SECONDS)
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            await _tick_once()
        except Exception:  # noqa: BLE001
            logger.exception("health worker tick failed")
        # Wait for either the interval elapsing or the stop_event being set,
        # whichever comes first. asyncio.wait_for raises TimeoutError on the
        # normal "interval elapsed" path; that's expected, not an error.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEALTH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
    logger.info("health worker stopped")
