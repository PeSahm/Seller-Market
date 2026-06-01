"""Background worker: daily broker-order reconciler.

Once per ``BROKER_ORDER_RECONCILE_INTERVAL_SECONDS`` (default 24h) it calls
:func:`app.services.broker_orders.reconcile_all_recent`, which pulls a rolling
recent window of ``GetOrders`` for every customer into ``broker_orders`` so the
Bot report + Excel export stay current without anyone clicking "Refresh from
broker". The full historical backfill remains the operator-triggered refresh.

Off by default — it makes external broker calls (each customer login may cost
a captcha/OCR solve). Enable with ``ENABLE_BROKER_ORDER_RECONCILER=true`` once
the mgmt host can reach the ``api-{broker}`` endpoints (see the DNS note in
CLAUDE.md). Per-customer failures are isolated and logged inside the service,
so one unreachable account never wedges the sweep.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 24 * 60 * 60  # daily
_DEFAULT_LOOKBACK_DAYS = 3


async def run_broker_order_reconcile_worker(
    stop_event: Optional[asyncio.Event] = None,
    *,
    interval_seconds: int = _DEFAULT_INTERVAL,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
) -> None:
    """Long-running loop. Stops when ``stop_event`` is set.

    Runs one reconcile sweep immediately on start, then every
    ``interval_seconds``. Because the data is idempotent and the window is a
    rolling ``lookback_days``, the exact wall-clock time of the tick doesn't
    matter — today's completed trades are always inside the window.
    """
    logger.info(
        "broker order reconciler started (interval=%ss, lookback=%dd)",
        interval_seconds, lookback_days,
    )
    stop_event = stop_event or asyncio.Event()
    try:
        while not stop_event.is_set():
            try:
                from app.services import broker_orders

                swept = await broker_orders.reconcile_all_recent(
                    lookback_days=lookback_days
                )
                logger.info("broker order reconcile swept %d customer(s)", swept)
            except Exception:  # noqa: BLE001 — never let one tick kill the worker
                logger.exception("broker order reconcile tick failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("broker order reconciler stopped")


__all__ = ["run_broker_order_reconcile_worker"]
