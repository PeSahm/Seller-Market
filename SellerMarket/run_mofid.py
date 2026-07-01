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
import os
import re
import threading
from datetime import datetime

import mofid_firer
import order_fire_log
from broker_adapters import get_adapter, is_auto_sell_only, resolve_family
from captcha_utils import decode_captcha
from cred_errors import InvalidCredentialsError
from log_rotation import rotate_and_truncate
from runtime_config import drop_non_customer_sections

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    """Configure file+stdout logging for the standalone run (called from main()).

    Kept OUT of module scope so importing run_mofid — e.g. bot_entrypoint's gate
    calling ``mofid_buy_targets`` — has NO side effects: no log rotation, and no
    reconfiguration of the long-lived bot process's root logger.
    """
    rotate_and_truncate("run_mofid.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("run_mofid.log", mode="a", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

# Per-section join timeout: cover the worst case of waiting from process start
# (~08:44) to the fire window (~08:45) plus login + drafts + the attempt budget.
_JOIN_TIMEOUT_S = 480.0

# Cross-restart idempotency. The scheduler re-runs a due job for 120s after its
# time, so a container restart inside 08:44:00–08:46:00 would re-launch run_mofid.
# To stop a re-created draft from DOUBLE-FIRING a real BUY, drop a per-(account,
# isin)-per-day marker in run_results/ (bind-mounted → survives a restart) once
# we've been through the fire window, and skip that section on any re-run.
_RUN_RESULTS_DIR = os.path.join(os.path.dirname(__file__), "run_results")


def _fire_latch_path(account: str, isin: str) -> str:
    today = datetime.now().strftime("%Y%m%d")
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{account}_{isin}")
    return os.path.join(_RUN_RESULTS_DIR, f"mofid_fired_{key}_{today}.marker")


def _fired_today(account: str, isin: str) -> bool:
    try:
        return os.path.exists(_fire_latch_path(account, isin))
    except Exception:  # noqa: BLE001 — a latch-read failure must never block firing
        return False


def _mark_fired_today(account: str, isin: str) -> None:
    try:
        os.makedirs(_RUN_RESULTS_DIR, exist_ok=True)
        with open(_fire_latch_path(account, isin), "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat())
    except OSError:
        logger.warning("mofid: failed to write fire latch for %s/%s", account, isin)


def _prune_fire_latches(max_age_days: int = 7) -> None:
    """Delete ``mofid_fired_*.marker`` older than ``max_age_days`` (housekeeping)."""
    import glob

    try:
        cutoff = datetime.now().timestamp() - max_age_days * 86400
        for path in glob.glob(os.path.join(_RUN_RESULTS_DIR, "mofid_fired_*.marker")):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — housekeeping must never block firing
        pass


def fire_section(name: str, section: dict) -> bool:
    """Fire one Mofid BUY section. Returns True on a confirmed fire. Never raises."""
    username = section.get("username", "?")
    broker = section.get("broker", "?")
    isin = section.get("isin", "?")
    side = int(section.get("side", 1))
    if _fired_today(username, isin):
        logger.info(
            "mofid %s (%s@%s): already handled today — skip (cross-restart idempotency)",
            isin, username, broker,
        )
        return False
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
        # Latch as soon as the batch has gone through the window — BEFORE we even
        # evaluate success — so a restart can never re-fire this (account, isin)
        # today, even if the broker accepted but our process died before we
        # recorded it. A pre-fire failure (login/draft) raises above this line and
        # is NOT latched, so a genuine early failure can still retry on a re-run.
        _mark_fired_today(username, isin)
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


def mofid_buy_targets(config_path: str = "config.ini") -> list:
    """The Mofid BUY sections ``[(name, section_dict), …]`` in ``config_path``.

    Shared by ``main()`` (to fire) and ``bot_entrypoint._start_mofid_scheduler``
    (to decide whether to launch the independent Mofid scheduler at all). A
    section qualifies iff it is a customer row (has ``username``), is NOT
    auto-sell-only, resolves to family ``mofid``, and is a BUY (``side == 1``).
    Never raises — a missing file yields no sections (``configparser`` skips it
    silently), and a malformed/unparseable config is caught → ``[]``. So the gate
    can call it safely at bot startup.
    """
    config = configparser.ConfigParser()
    try:
        config.read(config_path)
    except Exception:  # noqa: BLE001 — a malformed config must not crash the gate
        logger.exception("run_mofid: failed to read %s", config_path)
        return []
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
        try:
            side = int(section.get("side", 1))
        except (TypeError, ValueError):
            logger.warning("mofid section %s: bad side %r — skipped", name, section.get("side"))
            continue
        if side != 1:
            logger.info("mofid section %s is SELL — skipped (sells go via auto-sell)", name)
            continue
        targets.append((name, section))
    return targets


def main() -> None:
    _setup_logging()
    _prune_fire_latches()
    targets = mofid_buy_targets("config.ini")
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
            daemon=False,  # NON-daemon: a fire in flight must never be abruptly
        )                  # killed when main returns. The firer is internally
        t.start()          # bounded (window + max_attempts) and the scheduler's
        threads.append(t)  # subprocess timeout is the ultimate backstop.
    # Join until the fire window CLOSES (+ buffer), not a fixed 480s — a run_time
    # set well before the window (e.g. 08:30 for a 08:45 window) means the threads
    # legitimately wait far longer than 8 min for their window; a short join would
    # log a premature "0 fired" while the threads are still armed.
    try:
        join_timeout = max(
            _JOIN_TIMEOUT_S,
            (mofid_firer.window_end_local_ms() - int(datetime.now().timestamp() * 1000))
            / 1000.0 + 60.0,
        )
    except Exception:  # noqa: BLE001
        join_timeout = _JOIN_TIMEOUT_S
    for t in threads:
        t.join(timeout=join_timeout)

    fired = sum(1 for v in results.values() if v)
    logger.info("run_mofid: done — %d/%d fired", fired, len(targets))


if __name__ == "__main__":
    main()
