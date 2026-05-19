"""Background worker: scheduled-run marker ingestor.

Mirrors the structure of :mod:`app.workers.trade_ingestor`. One asyncio
task started at app boot iterates over every agent stack every
``SCHEDULED_RUN_INGEST_INTERVAL_SECONDS`` (default 30) and calls
:func:`app.services.scheduled_run_ingestor.ingest_stack_once` for each.

Each tick:
  * SSH-lists ``<stack_dir>/run_results/scheduled_run_*.json`` on the
    trading server.
  * For each marker, INSERT a new ``runs`` row (start marker) or UPDATE
    an existing one to a terminal status (final marker).
  * Delete the remote marker after a successful DB upsert so it can't
    be re-processed.

Per-stack concurrency capped at 4 so a fleet with many stacks doesn't
fan out to dozens of parallel SSH sessions.

See issue #62 for the design context.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.stacks import AgentStack

logger = logging.getLogger(__name__)


_DEFAULT_INTERVAL = 30
_PER_STACK_TIMEOUT_SECONDS = 60
_MAX_CONCURRENT_TICKS = 4


async def _check_one(stack_id: UUID) -> None:
    """Run one ingest tick for one stack with a hard timeout."""
    from app.services import scheduled_run_ingestor as services_ingestor

    try:
        async with AsyncSessionLocal() as db:
            result = await asyncio.wait_for(
                services_ingestor.ingest_stack_once(db, stack_id=stack_id),
                timeout=_PER_STACK_TIMEOUT_SECONDS,
            )
            if result.files_seen:
                logger.info(
                    "scheduled_run_ingest stack=%s seen=%d ins=%d upd=%d skip=%d errors=%d",
                    stack_id, result.files_seen, result.rows_inserted,
                    result.rows_updated, result.rows_skipped, len(result.errors),
                )
            for err in result.errors[:3]:
                logger.info("  err: %s", err)
    except asyncio.TimeoutError:
        logger.warning("scheduled_run_ingest stack=%s timed out", stack_id)
    except Exception:  # noqa: BLE001 — never let one stack fail the whole worker
        logger.exception("scheduled_run_ingest stack=%s unexpected error", stack_id)


async def _tick_all_stacks() -> None:
    """One full round: list every stack and dispatch checks under a sem cap."""
    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(AgentStack.id))
        ids = [r[0] for r in rows.all()]
    if not ids:
        return
    sem = asyncio.Semaphore(_MAX_CONCURRENT_TICKS)

    async def _bounded(stack_id: UUID) -> None:
        async with sem:
            await _check_one(stack_id)

    await asyncio.gather(*[_bounded(sid) for sid in ids])


async def run_scheduled_run_ingest_worker(
    stop_event: Optional[asyncio.Event] = None,
    *,
    interval_seconds: int = _DEFAULT_INTERVAL,
) -> None:
    """Long-running loop. Stops when ``stop_event`` is set.

    No kick-queue here yet — the trade ingestor has one because a manual
    run wants its order_results in the UI within seconds. Scheduled runs
    have no equivalent "user just clicked something" trigger; the next
    30-second tick is fine.
    """
    logger.info(
        "scheduled_run_ingest worker started (interval=%ss, max_concurrent=%d)",
        interval_seconds, _MAX_CONCURRENT_TICKS,
    )
    stop_event = stop_event or asyncio.Event()
    try:
        while not stop_event.is_set():
            try:
                await _tick_all_stacks()
            except Exception:  # noqa: BLE001
                logger.exception("scheduled_run_ingest worker tick failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("scheduled_run_ingest worker stopped")


__all__ = ["run_scheduled_run_ingest_worker"]
