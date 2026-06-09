"""Hermetic tests for rlc_ws pure helpers (#110, Phase 1).

The WebSocket I/O (websocket-client) is lazily imported inside RlcQueueClient, so
these tests run without the dependency. The MW wire is comma-separated text
(confirmed live). Run: ``python -m pytest test_rlc_ws.py -q``.
"""
from __future__ import annotations

import rlc_ws

# A real-shaped MW frame (trimmed): MW,<insCode>,<ISIN>,<name>,...
_MW = ("MW,11686,IRO1SROD0001,name,11150,10820,11480,N1,P,11150,"
       "x,y,z,w,IS,sym,1,1,100000,20260608090248").split(",")


# ---------------------------------------------------------------------------
# parse_mw
# ---------------------------------------------------------------------------

def test_parse_mw_only_market_watch_frames():
    assert rlc_ws.parse_mw("V,,,20260609,136") is None      # server-time frame
    assert rlc_ws.parse_mw("not a frame") is None
    assert rlc_ws.parse_mw(123) is None                      # non-str
    parts = rlc_ws.parse_mw("MW,11686,IRO1SROD0001,name,1")
    assert parts[0] == "MW" and parts[2] == "IRO1SROD0001"


# ---------------------------------------------------------------------------
# self-calibration: find the buy-queue INDEX by matching the REST bbq
# ---------------------------------------------------------------------------

def test_find_buy_queue_index_unique_match():
    parts = "MW,1,IRO1X,name,9930,9370,7922712,55".split(",")
    # 7922712 is the only field equal to the REST bbq.
    assert rlc_ws.find_buy_queue_index(parts, 7922712) == 6


def test_find_buy_queue_index_ambiguous_returns_none():
    parts = "MW,1,IRO1X,name,100,100".split(",")     # two fields == 100
    assert rlc_ws.find_buy_queue_index(parts, 100) is None


def test_find_buy_queue_index_zero_or_none_never_calibrates():
    # At market close bbq is 0 and many fields are 0 → never bind on it.
    assert rlc_ws.find_buy_queue_index("MW,1,X,n,0,0,0".split(","), 0) is None
    assert rlc_ws.find_buy_queue_index(_MW, None) is None


# ---------------------------------------------------------------------------
# extract_index
# ---------------------------------------------------------------------------

def test_extract_index():
    parts = "MW,1,IRO1X,name,9930,7922712".split(",")
    assert rlc_ws.extract_index(parts, 5) == 7922712
    assert rlc_ws.extract_index(parts, 3) is None     # 'name' is non-numeric
    assert rlc_ws.extract_index(parts, 99) is None    # out of range


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
