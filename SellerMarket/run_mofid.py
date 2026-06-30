"""Mofid / Orbis order-firing entry point — scheduled as ``python run_mofid.py``.

Mofid orders are bounded by a 1500-requests/hour cap, so they fire HERE (a
dedicated, bounded firer), NOT via the locust spam. For each Mofid **BUY**
section in ``config.ini``: pre-create the draft(s) + build the batch order off
the hot path, then fire the batch in the server-time-synced open window, stopping
at the first success (``mofid_firer``). One thread per section. Auto-sell-only,
SELL, and non-Mofid sections are skipped (handled by locust / the auto-sell
monitor). On a confirmed fire, append a side=1 fire-log line (no order id →
mgmt reconciles by date).

FLAT layout (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import configparser
import json
import logging
import threading

from log_rotation import rotate_and_truncate

rotate_and_truncate("run_mofid.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("run_mofid.log", mode="a", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

import mofid_firer
import order_fire_log
from broker_adapters import get_adapter, is_auto_sell_only, resolve_family
from captcha_utils import decode_captcha
from cred_errors import InvalidCredentialsError
from runtime_config import drop_non_customer_sections

# Per-section join timeout: cover the worst case of waiting from process start
# (~08:44) to the fire window (~08:45) plus login + drafts + the attempt budget.
_JOIN_TIMEOUT_S = 480.0


def fire_section(name: str, section: dict) -> bool:
    """Fire one Mofid BUY section. Returns True on a confirmed fire. Never raises."""
    username = section.get("username", "?")
    broker = section.get("broker", "?")
    isin = section.get("isin", "?")
    side = int(section.get("side", 1))
    try:
        adapter = get_adapter(
            broker,
            username=username,
            password=section.get("password", ""),
            config_section=section,
            captcha_decoder=decode_captcha,
        )
        # Pre-create the draft(s) + build the batch order (off the hot path).
        prepared = adapter.prepare_order(isin=isin, side=side, config_section=section)
        offset = adapter.server_time_offset_ms()
        start_hms, end_hms, max_attempts, interval = mofid_firer.window_config()
        ws, we = mofid_firer.compute_local_window_ms(start_hms, end_hms, offset)
        logger.info(
            "mofid %s (%s@%s): armed — window %s..%s (offset %sms), max_attempts=%d",
            isin, username, broker, start_hms, end_hms, offset, max_attempts,
        )
        result = mofid_firer.fire_batch_in_window(
            prepared,
            window_start_ms=ws,
            window_end_ms=we,
            max_attempts=max_attempts,
            interval_ms=interval,
        )
        if result.fired:
            try:
                resp = json.loads((result.body or b"").decode("utf-8", "replace") or "null")
            except Exception:  # noqa: BLE001
                resp = (result.body or b"").decode("utf-8", "replace")[:500]
            order_fire_log.emit_order_fire(
                username, broker, isin, side, order_response=resp
            )
            logger.info(
                "✓ mofid FIRED %s (%s@%s) in %d attempt(s)",
                isin, username, broker, result.attempts,
            )
            return True
        logger.warning(
            "✗ mofid NOT fired %s (%s@%s) after %d attempt(s)",
            isin, username, broker, result.attempts,
        )
        return False
    except InvalidCredentialsError:
        logger.warning("⚠ SKIP %s@%s — invalid credentials (broker rejected)", username, broker)
        return False
    except Exception as e:  # noqa: BLE001 — one bad section must not kill the others
        logger.error("❌ mofid fire failed for %s@%s %s: %s", username, broker, isin, e)
        return False


def main() -> None:
    config = configparser.ConfigParser()
    config.read("config.ini")
    drop_non_customer_sections(config)

    targets = []
    for name in config.sections():
        section = dict(config[name])
        if "username" not in section:
            continue
        if is_auto_sell_only(section):
            continue
        if resolve_family(section.get("broker", ""), section) != "mofid":
            continue
        if int(section.get("side", 1)) != 1:
            logger.info("mofid section %s is SELL — skipped (sells go via auto-sell)", name)
            continue
        targets.append((name, section))

    if not targets:
        logger.info("run_mofid: no Mofid BUY sections — nothing to fire")
        return

    logger.info("run_mofid: firing %d Mofid section(s): %s",
                len(targets), ", ".join(n for n, _ in targets))
    results: dict = {}
    threads = []
    for name, section in targets:
        t = threading.Thread(
            target=lambda n=name, s=section: results.__setitem__(n, fire_section(n, s)),
            name=f"mofid-{name}",
            daemon=True,
        )
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT_S)

    fired = sum(1 for v in results.values() if v)
    logger.info("run_mofid: done — %d/%d fired", fired, len(targets))


if __name__ == "__main__":
    main()
