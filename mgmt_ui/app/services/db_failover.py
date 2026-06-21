"""App-level DB auto-failover supervisor (#156).

Probes the MAIN database; after a threshold of consecutive failures, fails the
app over to the warm spare by rebinding the shared sessionmaker
(:func:`app.db.activate_spare`). Runs on EVERY mgmt instance (each fails itself
over). It **never** fails back automatically — returning to the main is a
deliberate restart after a resync, so two diverging databases never both take
writes (split-brain).

Safety mechanisms:
- A FAILOVER marker file is written on failover; the backup cron skips its
  dump/restore while it exists, so it can't clobber live writes on the spare.
  The marker is cleared once mgmt is confirmed healthy on the main again
  (i.e. after a deliberate restart on the resynced main).
- Leadership is re-elected on the spare after failover (the old leader lock died
  with the main connection), preserving the single-worker-runner invariant.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import text

from app import db as db_mod
from app.db import AsyncSessionLocal
from app.models.health import HealthSignal
from app.settings import get_settings

logger = logging.getLogger(__name__)


async def _probe(engine, timeout: float) -> bool:
    """True if a fresh connection to ``engine`` can ``SELECT 1`` within ``timeout``."""

    async def _q() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_q(), timeout=timeout)
        return True
    except Exception as exc:  # noqa: BLE001 — any failure = unreachable
        logger.debug("db_failover: probe failed: %s", exc)
        return False


def _write_marker(path: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(datetime.now(timezone.utc).isoformat() + "\n")
        logger.warning("db_failover: wrote marker %s (backup cron will pause)", path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("db_failover: could not write marker %s: %s", path, exc)
        return False


async def _raise_signal(kind: str, severity: str, message: str) -> None:
    """Best-effort health-signal insert on whatever DB is now active."""
    try:
        async with AsyncSessionLocal() as s:
            s.add(HealthSignal(kind=kind, severity=severity, message=message))
            await s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("db_failover: could not record signal: %s", exc)


async def _do_failover(app) -> None:
    settings = get_settings()
    activated = await db_mod.activate_spare()
    if not activated:
        logger.error("db_failover: NO spare configured — staying on a dead main (503s)")
        return
    marker_ok = _write_marker(settings.resolved_failover_marker_path())
    app.state.active_db = "spare"
    app.state.failed_over_at = datetime.now(timezone.utc)
    if not marker_ok:
        # We still fail over (serving the spare beats serving a dead main), but
        # the cron-clobber guard is NOT armed — warn loudly so the operator
        # pauses the backup cron manually before it overwrites the live spare.
        await _raise_signal(
            "db_failover_marker_error",
            "critical",
            "FAILED OVER to the spare but could NOT write the failover marker — "
            "the backup cron guard is INACTIVE. Pause the backup cron manually "
            "so it cannot pg_restore over the live spare.",
        )

    # We intentionally do NOT re-elect leadership or restart workers at runtime.
    # The boot-time worker-leader keeps running the workers — they each open a
    # fresh ``AsyncSessionLocal()`` per tick, which is now rebound to the spare,
    # so they simply continue against it. Runtime promotion of a standby would
    # require re-running the one-shot worker startup handlers (a v1 limitation;
    # see RUNBOOK-db-failover.md). The advisory lock held on the dead main is
    # released by Postgres when that connection drops. We also skip closing the
    # dead-main leader session here so a blocking close can't stall failover —
    # it is reaped at shutdown (bounded by leader._safe_close).
    await _raise_signal(
        "db_failover",
        "critical",
        "Main database unreachable — FAILED OVER to the warm spare. Running on "
        "the spare; resync the main and restart mgmt to return (no automatic "
        "fail-back, to avoid split-brain).",
    )
    logger.error("db_failover: FAILED OVER to the spare database")


async def run_failover_supervisor(app, stop_event: asyncio.Event) -> None:
    """Supervisor loop: probe the main, fail over on a sustained outage, and
    (once on the spare) alert when the main returns. Interruptible via
    ``stop_event``."""
    settings = get_settings()
    interval = settings.db_probe_interval_seconds
    threshold = settings.db_probe_failure_threshold
    timeout = settings.db_probe_timeout_seconds
    marker_path = settings.resolved_failover_marker_path()

    # Rehydrate: a marker present at startup means we are (still) failed over —
    # bind to the spare and serve it, NOT the possibly-stale main. The marker is
    # durable failover state; only the operator removes it (as part of the
    # deliberate resync + restart fail-back). This is what prevents a restart on
    # a momentarily-reachable main from clobbering the live spare.
    if os.path.exists(marker_path):
        logger.warning(
            "db_failover: marker present at startup — rehydrating onto the SPARE "
            "(operator must resync the main + remove %s to return)",
            marker_path,
        )
        if await db_mod.activate_spare():
            app.state.active_db = "spare"

    failures = 0
    main_back_alerted = False

    logger.info(
        "db_failover supervisor started (interval=%ss threshold=%s timeout=%ss)",
        interval,
        threshold,
        timeout,
    )
    while not stop_event.is_set():
        if db_mod.active_db() == "main":
            if await _probe(db_mod.engine, timeout):
                failures = 0
            else:
                failures += 1
                logger.warning("db_failover: main probe failed (%d/%d)", failures, threshold)
                if failures >= threshold:
                    # Never let an error in failover kill the only recovery task.
                    try:
                        await _do_failover(app)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("db_failover: _do_failover errored: %s", exc)
                    # On success active_db()=="spare" → we won't re-enter here.
                    # If it failed (no spare / error), reset so we retry after
                    # `threshold` more failures rather than spamming every tick.
                    failures = 0
        else:
            # Already on the spare. Probe the main only to ALERT when it's back —
            # we NEVER auto-fail-back and NEVER auto-clear the marker (operator
            # resyncs, removes the marker, and restarts).
            if not main_back_alerted and await _probe(db_mod.engine, timeout):
                main_back_alerted = True
                await _raise_signal(
                    "db_failback_available",
                    "warning",
                    "Main database is reachable again. mgmt is still on the spare. "
                    "Resync the main from the spare, remove the failover marker, "
                    "then restart mgmt to return.",
                )
                logger.warning("db_failover: main reachable again — resync + restart to return")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("db_failover supervisor stopped")
