"""Bounded, server-time-synced order firer for Mofid / Orbis.

Mofid enforces a **1500-requests/HOUR** cap, so its orders CANNOT ride the locust
spam (which fires ~1000+ POSTs per run). Each Mofid section fires here instead:
in the server-time-aligned open window, re-POST the prepared **batch** order,
STOPPING at the first success, bounded by a hard per-run attempt cap. One thread
per section (driven by ``run_mofid.py``); no locust, no fork → the cap is
trivially correct and can never be diluted by ``--processes`` workers.

All I/O (the sender, the clock, sleep) is injected so the window/cap/stop-on-
success logic is hermetically testable. FLAT layout (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Tuple

import direct_sell
import runtime_config
from broker_adapters import PreparedOrder
from mofid_adapter import mofid_response_ok

logger = logging.getLogger(__name__)

# Default server-time fire window (Tehran market open), overridable via [runtime].
WINDOW_START_DEFAULT = "08:44:58.450"
WINDOW_END_DEFAULT = "08:45:00.900"
MAX_ATTEMPTS_DEFAULT = 40
INTERVAL_MS_DEFAULT = 20


@dataclass
class FireResult:
    fired: bool
    attempts: int
    status: Optional[int] = None
    body: Optional[bytes] = None


def _hms_to_local_epoch_ms(hms: str, now_fn: Callable[[], datetime]) -> int:
    """Local epoch ms of today's wall-clock ``HH:MM:SS[.mmm]`` (in the bot TZ)."""
    parts = hms.strip().split(":")
    hour, minute = int(parts[0]), int(parts[1])
    sec_f = float(parts[2]) if len(parts) > 2 else 0.0
    sec = int(sec_f)
    micro = int(round((sec_f - sec) * 1_000_000))
    now = now_fn()
    target = now.replace(hour=hour, minute=minute, second=sec, microsecond=micro)
    return int(target.timestamp() * 1000)


def compute_local_window_ms(
    start_hms: str,
    end_hms: str,
    offset_ms: int,
    *,
    now_fn: Callable[[], datetime] = datetime.now,
) -> Tuple[int, int]:
    """Convert the SERVER-time window to LOCAL epoch ms.

    The broker clock leads the local clock by ``offset_ms`` (``diff`` from
    ``/easy/api/account/server-time``), so to act when the SERVER reads
    ``start_hms`` we fire at ``local_epoch(start_hms) - offset_ms`` (the Orbis.py
    math). Returns ``(start_ms, end_ms)`` in LOCAL epoch ms.
    """
    start = _hms_to_local_epoch_ms(start_hms, now_fn) - int(offset_ms)
    end = _hms_to_local_epoch_ms(end_hms, now_fn) - int(offset_ms)
    return start, end


def fire_batch_in_window(
    prepared: PreparedOrder,
    *,
    window_start_ms: int,
    window_end_ms: int,
    max_attempts: int = MAX_ATTEMPTS_DEFAULT,
    interval_ms: int = INTERVAL_MS_DEFAULT,
    send: Callable[[PreparedOrder], Tuple[int, bytes]] = direct_sell.send_prepared_order,
    ok: Callable[[int, bytes], bool] = mofid_response_ok,
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    sleep: Callable[[float], None] = time.sleep,
) -> FireResult:
    """Re-POST ``prepared`` (the batch order) inside ``[window_start_ms,
    window_end_ms)`` until the FIRST confirmed success, bounded by ``max_attempts``.

    * Before the window opens → short sleeps, no POST.
    * Each POST counts toward ``max_attempts`` (the hard 1500/hr backstop), spaced
      by ``interval_ms`` so the cap is a true ceiling, not a microsecond spin.
    * STOPS at the first ``ok(status, body)`` — so a steady-state run is one
      successful batch ≈ a handful of order-sends, far under 1500/hr.
    """
    attempts = 0
    last_status: Optional[int] = None
    last_body: Optional[bytes] = None
    while now_ms() < window_end_ms and attempts < max_attempts:
        if now_ms() < window_start_ms:
            sleep(0.05)
            continue
        try:
            status, body = send(prepared)
        except Exception as exc:  # noqa: BLE001 — transport error: count it, retry
            logger.warning("mofid fire attempt %d transport error: %s", attempts + 1, exc)
            attempts += 1
            sleep(interval_ms / 1000.0)
            continue
        attempts += 1
        last_status, last_body = status, body
        if ok(status, body):
            logger.info("mofid fire SUCCESS on attempt %d (HTTP %s)", attempts, status)
            return FireResult(True, attempts, status, body)
        sleep(interval_ms / 1000.0)
    logger.warning(
        "mofid fire NOT confirmed after %d attempt(s) (last HTTP %s)", attempts, last_status
    )
    return FireResult(False, attempts, last_status, last_body)


def window_config() -> Tuple[str, str, int, int]:
    """The fire-window settings (DB-pushable via [runtime])."""
    return (
        runtime_config.get("mofid_window_start", WINDOW_START_DEFAULT),
        runtime_config.get("mofid_window_end", WINDOW_END_DEFAULT),
        runtime_config.get_int("mofid_max_fire_attempts", MAX_ATTEMPTS_DEFAULT),
        runtime_config.get_int("mofid_fire_interval_ms", INTERVAL_MS_DEFAULT),
    )


__all__ = [
    "FireResult", "compute_local_window_ms", "fire_batch_in_window", "window_config",
]
