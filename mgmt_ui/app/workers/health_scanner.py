"""Background health-signal scanner.

One asyncio task started at app boot. Every
``HEALTH_SCAN_INTERVAL_SECONDS`` (default 60) iterate every agent stack
and call ``services.health_signals.scan_stack_once`` for each — that
function reads the last ~500 lines of trading_bot.log on the remote
host, runs the Phase 8.A regex catalogue, and upserts health_signals
rows (with 60-minute dedup window).
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
_PER_STACK_TIMEOUT_SECONDS = 30
_MAX_CONCURRENT = 8


async def _check_one(stack_id: UUID) -> None:
    from app.services import health_signals as svc

    try:
        async with AsyncSessionLocal() as db:
            result = await asyncio.wait_for(
                svc.scan_stack_once(db, stack_id=stack_id),
                timeout=_PER_STACK_TIMEOUT_SECONDS,
            )
            if result.error:
                logger.info(
                    "health_scan stack=%s err=%s", stack_id, result.error,
                )
            elif result.signals_inserted or result.signals_bumped:
                logger.info(
                    "health_scan stack=%s lines=%d inserted=%d bumped=%d",
                    stack_id, result.lines_scanned,
                    result.signals_inserted, result.signals_bumped,
                )
    except asyncio.TimeoutError:
        logger.warning("health_scan stack=%s timed out", stack_id)
    except Exception:  # noqa: BLE001
        logger.exception("health_scan stack=%s unexpected error", stack_id)


async def _tick_all_stacks() -> None:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(AgentStack.id))
        ids = [r[0] for r in rows.all()]
    if not ids:
        return
    sem = asyncio.Semaphore(_MAX_CONCURRENT)

    async def _bounded(sid: UUID) -> None:
        async with sem:
            await _check_one(sid)

    await asyncio.gather(*[_bounded(sid) for sid in ids])


async def run_health_scanner(
    stop_event: Optional[asyncio.Event] = None,
    *,
    interval_seconds: int = _DEFAULT_INTERVAL,
) -> None:
    if interval_seconds <= 0:
        # Same defensive guard as run_janitor — settings.py validates
        # env vars but a direct programmatic caller could still hand us
        # 0/negative, which would spin the loop tightly.
        raise ValueError(
            f"run_health_scanner: interval_seconds must be > 0 "
            f"(got {interval_seconds})"
        )
    logger.info(
        "health-scanner worker started (interval=%ss, max_concurrent=%d)",
        interval_seconds, _MAX_CONCURRENT,
    )
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            await _tick_all_stacks()
        except Exception:  # noqa: BLE001
            logger.exception("health-scanner tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue
    logger.info("health-scanner worker stopped")


__all__ = ["run_health_scanner"]
