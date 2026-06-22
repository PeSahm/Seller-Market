"""Background per-server service-reachability probe worker.

One asyncio task started at app boot (leader-gated). Every
``SERVICE_PROBE_INTERVAL_SECONDS`` (default 300) it SSH-probes every managed
server for every service it depends on and upserts the results into
``service_probe_results`` (rendered as the endpoint x server matrix on
``/admin/server-services``).

UNAUTHENTICATED only — it never logs into a broker. The authenticated "Deep
check" tier is manual (``service_monitor.deep_check_once``), triggered from the
page, so the worker carries zero captcha/rate-limit/lock cost.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.db import AsyncSessionLocal

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 300


async def _tick_once() -> None:
    from app.services import service_monitor as svc

    async with AsyncSessionLocal() as db:
        n = await svc.probe_all_once(db)
    logger.info("service_probe tick: probed %d server(s)", n)


async def run_service_probe_worker(
    stop_event: Optional[asyncio.Event] = None,
    *,
    interval_seconds: int = _DEFAULT_INTERVAL,
) -> None:
    if interval_seconds <= 0:
        raise ValueError(
            f"run_service_probe_worker: interval_seconds must be > 0 "
            f"(got {interval_seconds})"
        )
    logger.info("service-probe worker started (interval=%ss)", interval_seconds)
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            await _tick_once()
        except Exception:  # noqa: BLE001
            logger.exception("service-probe tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue
    logger.info("service-probe worker stopped")


__all__ = ["run_service_probe_worker"]
