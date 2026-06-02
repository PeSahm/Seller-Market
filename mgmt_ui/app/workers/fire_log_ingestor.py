"""Background worker: bot order fire-log ingestor.

Mirrors :mod:`app.workers.scheduled_run_ingestor`. Every
``FIRE_LOG_INGEST_INTERVAL_SECONDS`` it ingests each stack's
``run_results/order_fires_*.jsonl`` into ``order_fires``, then runs ONE
DB-only reconciliation pass that tags ``broker_orders.is_bot`` for executions
matching a fire. The reconciliation runs every tick (even with no new files)
because the matching executions usually arrive after the fire.
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

_DEFAULT_INTERVAL = 60
_PER_STACK_TIMEOUT_SECONDS = 60
_MAX_CONCURRENT_TICKS = 4


async def _check_one(stack_id: UUID) -> None:
    from app.services import fire_log_ingestor as svc

    try:
        async with AsyncSessionLocal() as db:
            result = await asyncio.wait_for(
                svc.ingest_stack_once(db, stack_id=stack_id),
                timeout=_PER_STACK_TIMEOUT_SECONDS,
            )
            if result.files_seen:
                logger.info(
                    "fire_log_ingest stack=%s files=%d ins=%d skip=%d errors=%d",
                    stack_id, result.files_seen, result.rows_inserted,
                    result.rows_skipped, len(result.errors),
                )
            for err in result.errors[:3]:
                logger.info("  err: %s", err)
    except asyncio.TimeoutError:
        logger.warning("fire_log_ingest stack=%s timed out", stack_id)
    except Exception:  # noqa: BLE001
        logger.exception("fire_log_ingest stack=%s unexpected error", stack_id)


async def _tick_all_stacks() -> None:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(AgentStack.id))
        ids = [r[0] for r in rows.all()]
    if ids:
        sem = asyncio.Semaphore(_MAX_CONCURRENT_TICKS)

        async def _bounded(stack_id: UUID) -> None:
            async with sem:
                await _check_one(stack_id)

        await asyncio.gather(*[_bounded(sid) for sid in ids])

    # Reconciliation pass — DB only, runs every tick because the matching
    # broker_orders executions usually land after the fire.
    from app.services import fire_log_ingestor as svc

    try:
        async with AsyncSessionLocal() as db:
            tagged = await svc.reconcile_unreconciled(db)
            if tagged:
                logger.info("fire_log reconcile: %d fire(s) reconciled", tagged)
    except Exception:  # noqa: BLE001
        logger.exception("fire_log reconcile pass failed")


async def run_fire_log_ingest_worker(
    stop_event: Optional[asyncio.Event] = None,
    *,
    interval_seconds: int = _DEFAULT_INTERVAL,
) -> None:
    """Long-running loop. Stops when ``stop_event`` is set."""
    logger.info(
        "fire_log_ingest worker started (interval=%ss, max_concurrent=%d)",
        interval_seconds, _MAX_CONCURRENT_TICKS,
    )
    stop_event = stop_event or asyncio.Event()
    try:
        while not stop_event.is_set():
            try:
                await _tick_all_stacks()
            except Exception:  # noqa: BLE001
                logger.exception("fire_log_ingest worker tick failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("fire_log_ingest worker stopped")


__all__ = ["run_fire_log_ingest_worker"]
