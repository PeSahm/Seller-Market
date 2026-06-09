"""Hermetic tests for rlc_ws pure helpers (#110, Phase 1).

The WebSocket I/O (websocket-client) is lazily imported inside RlcQueueClient, so
these tests run without the dependency. Run: ``python -m pytest test_rlc_ws.py -q``.
"""
from __future__ import annotations

import rlc_ws


# ---------------------------------------------------------------------------
# parse_frame
# ---------------------------------------------------------------------------

def test_parse_frame_skips_control_and_garbage():
    assert rlc_ws.parse_frame("not json") is None
    assert rlc_ws.parse_frame('{"msgType":"connect"}') is None
    assert rlc_ws.parse_frame('{"msgType":"time","time":1}') is None
    f = rlc_ws.parse_frame('{"msgType":"MW","bbq":1234}')
    assert f == {"msgType": "MW", "bbq": 1234}


# ---------------------------------------------------------------------------
# self-calibration: find the buy-queue field by matching the REST bbq
# ---------------------------------------------------------------------------

def test_find_buy_queue_field_top_level():
    frame = {"msgType": "MW", "bbq": 1234, "bsq": 50, "ltp": 9930}
    assert rlc_ws.find_buy_queue_field(frame, 1234) == "bbq"


def test_find_buy_queue_field_nested():
    frame = {"msgType": "MW", "queue": {"buy": 777, "sell": 9}}
    assert rlc_ws.find_buy_queue_field(frame, 777) == "queue.buy"


def test_find_buy_queue_field_none_when_no_match_or_no_expected():
    assert rlc_ws.find_buy_queue_field({"a": 1}, 999) is None
    assert rlc_ws.find_buy_queue_field({"a": 1}, None) is None
    # booleans must never be matched as a numeric queue value.
    assert rlc_ws.find_buy_queue_field({"flag": True}, 1) is None


def test_find_buy_queue_field_none_when_ambiguous():
    # Two fields share the value → ambiguous → don't bind (wait for a clean frame).
    assert rlc_ws.find_buy_queue_field({"bbq": 100, "other": 100}, 100) is None


# ---------------------------------------------------------------------------
# extract_field
# ---------------------------------------------------------------------------

def test_extract_field_top_and_nested():
    assert rlc_ws.extract_field({"bbq": 1234}, "bbq") == 1234
    assert rlc_ws.extract_field({"queue": {"buy": 777}}, "queue.buy") == 777
    assert rlc_ws.extract_field({"bbq": "55"}, "bbq") == 55      # numeric string coerced
    assert rlc_ws.extract_field({}, "bbq") is None
    assert rlc_ws.extract_field({"queue": {}}, "queue.buy") is None


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
