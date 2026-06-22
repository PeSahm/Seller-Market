"""Background worker: daily credential checker.

Once per day (default noon Tehran) it re-verifies every customer's broker
credentials and writes the verdict to ``customers.credential_status`` via
:func:`app.services.customers.set_credential_status` — powering the dashboard
"needs attention" metric and giving the agent a self-serve signal that a
password needs fixing.

No cron exists in this codebase, so "noon daily" is a time-gate inside a short
interval loop: the loop wakes every ``interval_seconds`` (cheap), and fires a
sweep only when the Tehran hour matches ``hour`` AND it hasn't already run today
(an in-memory Tehran-date latch makes it at-most-once-per-day even though the
hour window spans several wakes).

Off by default — each customer login may cost a captcha/OCR solve, so it makes
external broker calls. Enable with ``ENABLE_CREDENTIAL_CHECKER=true`` once the
mgmt host can reach the broker hosts. The TRANSIENT-sticky rule lives in
``set_credential_status`` (an OCR/broker outage at noon never blanks yesterday's
verdict). Per-customer failures are isolated so one bad account never wedges
the sweep.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Tehran is a fixed UTC+03:30 (no DST since 2022) — a plain fixed offset avoids a
# tzdata dependency. Same constant used elsewhere in the codebase.
_TEHRAN = timezone(timedelta(hours=3, minutes=30))

_DEFAULT_INTERVAL = 600  # wake every ~10 min; cheap when not firing
_DEFAULT_HOUR = 12  # noon Tehran


async def _sweep_once() -> tuple[int, int, int]:
    """Verify every customer once. Returns (checked, valid, invalid)."""
    from app.db import AsyncSessionLocal
    from app.services import broker_client
    from app.services import customers as customers_svc
    from app.services import settings_store
    from app.services.brokers.base import CredStatus, resolve_cred_status

    checked = valid = invalid = 0
    async with AsyncSessionLocal() as db:
        ocr_service_url = await settings_store.get_setting(db, "ocr_service_url")
        customers = await customers_svc.list_customers(db)

    for cust in customers:
        try:
            # Each customer gets its own short-lived session so one failure /
            # rollback can't poison the others.
            async with AsyncSessionLocal() as db:
                password = await customers_svc.decrypt_password(cust)
                result = await broker_client.verify_credentials(
                    broker_code=cust.broker,
                    username=cust.username,
                    password=password,
                    ocr_service_url=ocr_service_url,
                )
                status = resolve_cred_status(result)
                message = result.error or result.message
                await customers_svc.set_credential_status(
                    db, cust.id, status, message
                )
            checked += 1
            if status == CredStatus.VALID:
                valid += 1
            elif status == CredStatus.INVALID_CREDENTIALS:
                invalid += 1
        except Exception:  # noqa: BLE001 — isolate per-customer failures
            logger.exception("credential check failed for customer %s", cust.id)
    return checked, valid, invalid


async def run_credential_checker(
    stop_event: Optional[asyncio.Event] = None,
    *,
    interval_seconds: int = _DEFAULT_INTERVAL,
    target_hour: int = _DEFAULT_HOUR,
) -> None:
    """Long-running loop. Stops when ``stop_event`` is set.

    Fires at most once per Tehran day, when the Tehran hour first matches
    ``target_hour``. On a leader restart mid-day the latch resets in memory —
    a duplicate sweep is harmless (idempotent) so that's acceptable.
    """
    logger.info(
        "credential checker started (interval=%ss, hour=%02d Tehran)",
        interval_seconds, target_hour,
    )
    stop_event = stop_event or asyncio.Event()
    last_run_date = None  # Tehran date of the last completed sweep
    try:
        while not stop_event.is_set():
            try:
                now = datetime.now(_TEHRAN)
                if now.hour == target_hour and now.date() != last_run_date:
                    logger.info("credential checker: starting daily sweep")
                    checked, valid, invalid = await _sweep_once()
                    last_run_date = now.date()
                    logger.info(
                        "credential checker swept %d customer(s): "
                        "%d valid, %d invalid",
                        checked, valid, invalid,
                    )
            except Exception:  # noqa: BLE001 — never let one tick kill the worker
                logger.exception("credential checker tick failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("credential checker stopped")


__all__ = ["run_credential_checker"]
