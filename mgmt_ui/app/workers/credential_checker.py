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


async def _verify_one(db, cust, ocr_service_url):
    """Verify one customer against its broker and persist the verdict; return
    the :class:`CredStatus`."""
    from app.services import broker_client
    from app.services import customers as customers_svc
    from app.services.brokers.base import resolve_cred_status

    password = await customers_svc.decrypt_password(cust)
    result = await broker_client.verify_credentials(
        broker_code=cust.broker,
        username=cust.username,
        password=password,
        ocr_service_url=ocr_service_url,
    )
    status = resolve_cred_status(result)
    await customers_svc.set_credential_status(
        db, cust.id, status, result.error or result.message
    )
    return status


async def _verify_batch(customers, ocr_service_url, *, pace_seconds, sleep_fn):
    """Verify a list of customers, each in its own short-lived session (so one
    failure/rollback can't poison the others) and paced between calls to ease
    the broker identity-service rate limit that a bulk sweep otherwise trips.

    Returns ``(checked, valid, invalid, still_transient_ids)`` — an account that
    erred or came back inconclusive is reported as still-transient so the retry
    pass can pick it up.
    """
    from app.db import AsyncSessionLocal
    from app.services.brokers.base import CredStatus

    checked = valid = invalid = 0
    still_transient: list = []
    for cust in customers:
        try:
            async with AsyncSessionLocal() as db:
                status = await _verify_one(db, cust, ocr_service_url)
            checked += 1
            if status == CredStatus.VALID:
                valid += 1
            elif status == CredStatus.INVALID_CREDENTIALS:
                invalid += 1
            else:
                still_transient.append(cust.id)
        except Exception:  # noqa: BLE001 — isolate per-customer failures
            logger.exception("credential check failed for customer %s", cust.id)
            still_transient.append(cust.id)
        if pace_seconds > 0:
            await sleep_fn(pace_seconds)
    return checked, valid, invalid, still_transient


async def recheck_transients(
    *,
    rounds: int,
    cooldown_seconds: float,
    pace_seconds: float = 0.0,
    sleep_fn=asyncio.sleep,
) -> dict:
    """Re-verify ONLY the customers currently ``transient``.

    A ``transient`` verdict means the last check couldn't decide — almost always
    a rate-limit casualty of the bulk sweep, not a real problem. This re-verifies
    just that set, up to ``rounds`` passes each preceded by ``cooldown_seconds``
    so the rate limit clears. A transient that now resolves to valid/invalid is
    overwritten (the sticky rule only protects valid/invalid FROM a transient,
    not the reverse); one still undecided stays for the next round / next daily
    sweep. Bounded so a genuinely-unreachable broker can't loop forever.
    """
    from app.db import AsyncSessionLocal
    from app.services import customers as customers_svc
    from app.services import settings_store

    rounds_run = resolved = 0
    for _ in range(max(0, rounds)):
        async with AsyncSessionLocal() as db:
            ocr_service_url = await settings_store.get_setting(db, "ocr_service_url")
            transients = await customers_svc.list_customers(
                db, credential_status="transient"
            )
        if not transients:
            break
        if cooldown_seconds > 0:
            await sleep_fn(cooldown_seconds)  # let the broker rate limit clear
        before = len(transients)
        _, _, _, still = await _verify_batch(
            transients, ocr_service_url, pace_seconds=pace_seconds, sleep_fn=sleep_fn
        )
        rounds_run += 1
        resolved += before - len(still)
        logger.info(
            "transient recheck round %d: %d rechecked, %d resolved, %d still transient",
            rounds_run, before, before - len(still), len(still),
        )
        if not still:
            break
    return {"rounds": rounds_run, "resolved": resolved}


async def _sweep_once(
    *,
    pace_seconds: float = 0.0,
    retry_rounds: int = 0,
    retry_cooldown_seconds: float = 0.0,
    sleep_fn=asyncio.sleep,
) -> tuple[int, int, int]:
    """Verify every customer once (paced), then re-check the transients so a
    rate-limit casualty of this very sweep self-heals. Returns the FIRST-pass
    ``(checked, valid, invalid)``; ``recheck_transients`` logs its own progress.
    """
    from app.db import AsyncSessionLocal
    from app.services import customers as customers_svc
    from app.services import settings_store

    async with AsyncSessionLocal() as db:
        ocr_service_url = await settings_store.get_setting(db, "ocr_service_url")
        customers = await customers_svc.list_customers(db)
    checked, valid, invalid, _still = await _verify_batch(
        customers, ocr_service_url, pace_seconds=pace_seconds, sleep_fn=sleep_fn
    )
    if retry_rounds > 0:
        await recheck_transients(
            rounds=retry_rounds,
            cooldown_seconds=retry_cooldown_seconds,
            pace_seconds=pace_seconds,
            sleep_fn=sleep_fn,
        )
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
                    from app.settings import get_settings
                    s = get_settings()
                    logger.info("credential checker: starting daily sweep")
                    checked, valid, invalid = await _sweep_once(
                        pace_seconds=s.credential_check_pace_seconds,
                        retry_rounds=s.credential_check_retry_rounds,
                        retry_cooldown_seconds=s.credential_check_retry_cooldown_seconds,
                    )
                    last_run_date = now.date()
                    logger.info(
                        "credential checker swept %d customer(s): "
                        "%d valid, %d invalid (transients re-checked after cooldown)",
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
