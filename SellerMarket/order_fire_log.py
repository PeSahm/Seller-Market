"""Append-only order fire-log writer (#110 auto-sell).

A standalone copy of the bot's fire-log record so the auto-sell monitor can emit
side=2 (SELL) fires WITHOUT importing ``locustfile_new`` (which has import-time
side effects — it truncates ``trading_bot.log`` and builds locust user classes).

The schema, filename, and append semantics are **byte-identical** to
``locustfile_new._emit_order_fire``, so both sources append to the same
``run_results/order_fires_<YYYYMMDD>.jsonl`` and the mgmt ``fire_log_ingestor``
reads them uniformly (dedup on ``fire_uid``; reconciliation already supports
side=2). A single small JSON line in O_APPEND mode is atomic across processes.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

FIRE_LOG_SCHEMA = 1
RUN_RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_results")


def emit_order_fire(
    username: str,
    broker_code: str,
    isin: str,
    side: int,
    *,
    serial_number: Optional[int] = None,
    tracking_number: Optional[int] = None,
    order_response: Any = None,
    run_results_dir: Optional[str] = None,
) -> None:
    """Append one order-fire record. NEVER raises (fire-log must not break trading).

    Matches ``locustfile_new._emit_order_fire`` field-for-field so the ingestor
    treats monitor-written SELL fires identically to locust-written BUY fires.
    """
    try:
        directory = run_results_dir or RUN_RESULTS_DIR
        os.makedirs(directory, exist_ok=True)
        now = datetime.now(timezone.utc)
        record = {
            "schema_version": FIRE_LOG_SCHEMA,
            "fire_uid": uuid.uuid4().hex,
            "username": username,
            "broker_code": broker_code,
            "isin": isin,
            "side": side,
            "fired_at": now.isoformat(),
            "serial_number": serial_number,
            "tracking_number": tracking_number,
            "order_response": order_response,
        }
        path = os.path.join(directory, f"order_fires_{now.strftime('%Y%m%d')}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — fire-log I/O must never break trading
        # Log so a persistent failure (bad mount, full disk) is visible, but
        # NEVER re-raise — a missed fire-log line must not abort a real sell.
        logger.warning("order_fire_log: failed to append fire for %s/%s side=%s",
                       broker_code, isin, side, exc_info=True)


__all__ = ["FIRE_LOG_SCHEMA", "emit_order_fire"]
