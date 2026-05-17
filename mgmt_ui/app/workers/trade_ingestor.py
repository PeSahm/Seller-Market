"""Background trade-result ingestor.

Mirrors the structure of ``app.workers.health`` (Phase 2). One asyncio
task started at app boot iterates over every agent stack every
``TRADE_INGEST_INTERVAL_SECONDS`` and calls
``services.trade_ingestor.ingest_stack_once`` for each.

A per-tick concurrency cap (``_MAX_CONCURRENT_TICKS = 8``) prevents
fan-out blowup when a fleet has many stacks — the parser/upsert is
DB-heavy, and 8 parallel SSH sessions is plenty for the use case.
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
_MAX_CONCURRENT_TICKS = 8


# Background-task-friendly queue so the run_executor (or any other
# caller) can kick an immediate ingest for a specific stack without
# waiting for the next polling tick. Drained inside ``run_trade_ingest_worker``.
_KICK_QUEUE: asyncio.Queue[UUID] = asyncio.Queue()


def kick_ingest_for_stack(stack_id: UUID) -> None:
    """Schedule an out-of-band ingest tick for ``stack_id``.

    Best-effort; if the queue is full or no event loop is running we log
    and drop. Used by ``_run_executor_loop`` so a manual run's
    order_results show up in the UI in seconds instead of waiting up to
    the next ~30s tick.
    """
    try:
        _KICK_QUEUE.put_nowait(stack_id)
    except asyncio.QueueFull:
        logger.warning("trade_ingest kick queue full; dropping stack=%s", stack_id)
    except RuntimeError:
        # No event loop (e.g. import time, or shutdown).
        logger.debug("trade_ingest kick deferred; no event loop")


async def _check_one(stack_id: UUID) -> None:
    """Run one tick for one stack with a hard timeout."""
    # Lazy import so the workers package can load even if the service module
    # blows up at import time (e.g. missing optional dep).
    from app.services import trade_ingestor as services_trade_ingestor

    try:
        async with AsyncSessionLocal() as db:
            result = await asyncio.wait_for(
                services_trade_ingestor.ingest_stack_once(db, stack_id=stack_id),
                timeout=_PER_STACK_TIMEOUT_SECONDS,
            )
            if result.error:
                logger.info(
                    "trade_ingest stack=%s tick error: %s",
                    stack_id, result.error,
                )
            elif result.files_seen:
                logger.info(
                    "trade_ingest stack=%s seen=%d ingested=%d "
                    "inserted=%d duplicate=%d unmatched=%d",
                    stack_id, result.files_seen, result.files_ingested,
                    result.orders_inserted, result.orders_duplicate,
                    result.orders_unmatched_customer,
                )
    except asyncio.TimeoutError:
        logger.warning("trade_ingest stack=%s timed out", stack_id)
    except Exception:  # noqa: BLE001 — never let one stack fail the whole worker
        logger.exception("trade_ingest stack=%s unexpected error", stack_id)


async def _tick_all_stacks() -> None:
    """One full round: list every stack and dispatch checks with a concurrency cap."""
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


async def _drain_kicks() -> None:
    """Drain the kick queue between polling ticks.

    Run as a side task so it doesn't block the polling cadence. Each
    drained stack is ticked through the same ``_check_one`` path
    (which has its own timeout and exception swallow).
    """
    while True:
        stack_id = await _KICK_QUEUE.get()
        try:
            logger.info("trade_ingest kick received for stack=%s", stack_id)
            await _check_one(stack_id)
        finally:
            _KICK_QUEUE.task_done()


async def run_trade_ingest_worker(
    stop_event: Optional[asyncio.Event] = None,
    *,
    interval_seconds: int = _DEFAULT_INTERVAL,
) -> None:
    """Long-running loop. Stops when ``stop_event`` is set."""
    logger.info(
        "trade_ingest worker started (interval=%ss, max_concurrent=%d)",
        interval_seconds, _MAX_CONCURRENT_TICKS,
    )
    stop_event = stop_event or asyncio.Event()

    kick_task = asyncio.create_task(_drain_kicks(), name="trade-ingest-kicks")

    try:
        while not stop_event.is_set():
            try:
                await _tick_all_stacks()
            except Exception:  # noqa: BLE001
                logger.exception("trade_ingest worker tick failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue
    finally:
        kick_task.cancel()
        try:
            await kick_task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("trade_ingest worker stopped")


__all__ = ["run_trade_ingest_worker", "kick_ingest_for_stack"]
