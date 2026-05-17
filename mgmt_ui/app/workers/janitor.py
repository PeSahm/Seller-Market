"""Background janitor worker.

Runs once per ``JANITOR_INTERVAL_SECONDS`` (default 3600) and calls
``services.janitor.run_janitor_tick`` with retention knobs from
settings. The tick handles its own per-stack iteration and error
swallowing; this worker just paces it.
"""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from typing import Optional

from app.db import AsyncSessionLocal
from app.settings import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 3600
_TICK_TIMEOUT_SECONDS = 30 * 60  # generous; a slow `find` over many files shouldn't block forever


async def _do_tick() -> None:
    from app.services import janitor as svc

    s = get_settings()
    try:
        async with AsyncSessionLocal() as db:
            result = await asyncio.wait_for(
                svc.run_janitor_tick(
                    db,
                    run_logs_dir=Path(s.run_logs_dir),
                    order_results_retention_days=s.janitor_order_results_retention_days,
                    run_log_retention_days=s.janitor_run_log_retention_days,
                    health_signal_retention_days=s.janitor_health_signal_retention_days,
                ),
                timeout=_TICK_TIMEOUT_SECONDS,
            )
            logger.info(
                "janitor tick: order_results=%d stacks scanned, "
                "run_logs deleted=%d, health_signals deleted=%d",
                len(result.order_results),
                result.run_logs.files_deleted,
                result.health_signals.rows_deleted,
            )
    except asyncio.TimeoutError:
        logger.warning("janitor tick timed out")
    except Exception:  # noqa: BLE001
        logger.exception("janitor tick failed")


async def run_janitor(
    stop_event: Optional[asyncio.Event] = None,
    *,
    interval_seconds: int = _DEFAULT_INTERVAL,
) -> None:
    logger.info("janitor worker started (interval=%ss)", interval_seconds)
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        await _do_tick()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue
    logger.info("janitor worker stopped")


__all__ = ["run_janitor"]
