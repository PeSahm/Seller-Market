"""Pure-function tests for the fire-log ingestor's parsing.

The SSH + DB upsert/reconcile paths need Postgres and are covered by the
end-to-end verification; here we pin the JSONL → ``order_fires`` mapping (a
wrong field name silently loses the bot-attribution signal).
"""
from __future__ import annotations

import uuid
from datetime import date

from app.services.fire_log_ingestor import (
    _parse_fired_at,
    _row_values,
    _status_rows_from_marker,
)

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


# ---------------------------------------------------------------------------
# auto-sell hot-reload status marker (#110)
# ---------------------------------------------------------------------------

_STACK = uuid.uuid4()


def test_status_marker_happy_path():
    body = (
        '{"schema":1,"applied_at":"2026-06-10T11:42:00+00:00",'
        '"armed":[{"account":"u","isin":"IRO1A","threshold":500},'
        '{"account":"u2","isin":"IRO1B","threshold":300}]}'
    )
    rows = _status_rows_from_marker(body, _STACK)
    assert rows is not None and len(rows) == 2
    a = {r["isin"]: r for r in rows}
    assert a["IRO1A"]["applied_threshold"] == 500
    assert a["IRO1A"]["stack_id"] == _STACK
    assert a["IRO1A"]["account"] == "u"
    assert a["IRO1A"]["applied_at"].tzinfo is not None


def test_status_marker_same_isin_two_accounts_keeps_both():
    # Two customers on ONE stack arming the SAME instrument is normal fleet
    # state — the per-account key must keep both rows (a PK collision here
    # previously poisoned the whole ingest transaction).
    body = (
        '{"schema":1,"applied_at":"2026-06-10T11:42:00Z",'
        '"armed":[{"account":"u1","isin":"IRO1A","threshold":500},'
        '{"account":"u2","isin":"IRO1A","threshold":700}]}'
    )
    rows = _status_rows_from_marker(body, _STACK)
    assert len(rows) == 2
    by_acc = {r["account"]: r["applied_threshold"] for r in rows}
    assert by_acc == {"u1": 500, "u2": 700}


def test_status_marker_exact_duplicate_entries_deduped():
    # A pathological marker repeating the SAME (account, isin) must collapse to
    # one row (last wins) — never a bulk-INSERT PK violation.
    body = (
        '{"schema":1,"applied_at":"2026-06-10T11:42:00Z",'
        '"armed":[{"account":"u","isin":"IRO1A","threshold":500},'
        '{"account":"u","isin":"IRO1A","threshold":900}]}'
    )
    rows = _status_rows_from_marker(body, _STACK)
    assert len(rows) == 1
    assert rows[0]["applied_threshold"] == 900


def test_status_marker_disarm_all_is_empty_list_not_none():
    body = '{"schema":1,"applied_at":"2026-06-10T11:42:00Z","armed":[]}'
    rows = _status_rows_from_marker(body, _STACK)
    assert rows == []          # valid "nothing armed" — caller will clear the stack


def test_status_marker_invalid_returns_none():
    # None ⇒ caller leaves existing rows untouched (don't wipe on a torn/garbage read).
    assert _status_rows_from_marker("not json", _STACK) is None
    assert _status_rows_from_marker('{"schema":2,"armed":[]}', _STACK) is None      # wrong schema
    assert _status_rows_from_marker('{"schema":1,"armed":[]}', _STACK) is None      # no applied_at
    assert _status_rows_from_marker('{"schema":1,"applied_at":"x","armed":[]}', _STACK) is None


def test_status_marker_skips_malformed_entries():
    body = (
        '{"schema":1,"applied_at":"2026-06-10T11:42:00Z","armed":['
        '{"account":"u","isin":"IRO1A","threshold":500},'
        '{"account":"u","isin":"IRO1B","threshold":"oops"},'   # non-int → skipped
        '{"account":"u","threshold":700},'                       # no isin → skipped
        '{"isin":"IRO1C","threshold":700},'                      # no account → skipped
        '"junk"]}'                                                # non-dict → skipped
    )
    rows = _status_rows_from_marker(body, _STACK)
    assert [r["isin"] for r in rows] == ["IRO1A"]
