"""Pure-function tests for the fire-log ingestor's parsing.

The SSH + DB upsert/reconcile paths need Postgres and are covered by the
end-to-end verification; here we pin the JSONL → ``order_fires`` mapping (a
wrong field name silently loses the bot-attribution signal).
"""
from __future__ import annotations

import uuid
from datetime import date

from app.services.fire_log_ingestor import _parse_fired_at, _row_values

_AGENT = uuid.uuid4()
_CUST = uuid.uuid4()


def _rec(**over):
    base = {
        "schema_version": 1,
        "fire_uid": "abc123",
        "username": "4580090306",
        "broker_code": "ayandeh",
        "isin": "IRO1PNES0001",
        "side": 1,
        "fired_at": "2026-06-01T05:10:00+00:00",
        "serial_number": 3300000000589570,
        "tracking_number": None,
    }
    base.update(over)
    return base


def test_row_values_happy_path():
    v = _row_values(_rec(), agent_id=_AGENT, customer_id=_CUST)
    assert v is not None
    assert v["customer_id"] == _CUST
    assert v["agent_id"] == _AGENT
    assert v["broker"] == "ayandeh"
    assert v["account_username"] == "4580090306"
    assert v["isin"] == "IRO1PNES0001"
    assert v["side"] == 1
    assert v["fire_uid"] == "abc123"
    assert v["run_date"] == date(2026, 6, 1)
    assert v["tracking_number"] is None


def test_row_values_keeps_tracking_number_when_present():
    v = _row_values(_rec(tracking_number=909), agent_id=_AGENT, customer_id=_CUST)
    assert v["tracking_number"] == 909


def test_row_values_captures_serial_number():
    v = _row_values(_rec(), agent_id=_AGENT, customer_id=_CUST)
    assert v["serial_number"] == 3300000000589570
    # null serial is allowed (queue-style responses)
    assert _row_values(_rec(serial_number=None), agent_id=_AGENT, customer_id=None)["serial_number"] is None


def test_row_values_rejects_wrong_schema():
    assert _row_values(_rec(schema_version=2), agent_id=_AGENT, customer_id=None) is None


def test_row_values_rejects_missing_required_fields():
    for missing in ("fire_uid", "username", "broker_code", "isin", "fired_at"):
        assert _row_values(_rec(**{missing: None}), agent_id=_AGENT, customer_id=None) is None


def test_row_values_rejects_non_numeric_side():
    assert _row_values(_rec(side="buy"), agent_id=_AGENT, customer_id=None) is None


def test_row_values_allows_null_customer():
    v = _row_values(_rec(), agent_id=_AGENT, customer_id=None)
    assert v is not None and v["customer_id"] is None


def test_parse_fired_at_handles_z_suffix_and_naive():
    assert _parse_fired_at("2026-06-01T05:10:00Z").tzinfo is not None
    naive = _parse_fired_at("2026-06-01T05:10:00")
    assert naive is not None and naive.tzinfo is not None  # assumed UTC
    assert _parse_fired_at(None) is None
    assert _parse_fired_at("garbage") is None
