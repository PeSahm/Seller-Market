"""Hermetic tests for rlc_ws pure helpers (#110).

The WebSocket I/O (websocket-client) is lazily imported inside RlcQueueClient, so
these tests run without the dependency. The MW wire is comma-separated text with
the order-book depth in semicolon-packed ``bl1``/``bl2``/``bl3`` blobs — the
fixture below is a REAL frame captured live (2026-06-10, IRT3SORF0001; the bl1
volume 31,706,729 matched the REST ``bbq`` exactly).
Run: ``python -m pytest test_rlc_ws.py -q``.
"""
from __future__ import annotations

import rlc_ws

# Verbatim live frame (only the Persian company/symbol names matter as opaque text).
_LIVE_MW = (
    "MW,3867,IRT3SORF0001,صندوق س.بخشي صنايع سورنا2,21277,20039,21277,T1,N,"
    "21277,13934005,21277,21277,20658,,سورنافود1,1,1,1000000,20260610102559,"
    "bl1;31706729;21277;117;11:30:36;0;0;0;11:30:36,"
    "bl2;139338;20658;2;10:49:48;0;0;0;10:49:48,"
    "bl3;1453;20600;1;10:38:38;0;0;0;10:38:38,"
    "Sorena Ind.2 ETF,SORF1,21277"
)


# ---------------------------------------------------------------------------
# parse_mw
# ---------------------------------------------------------------------------

def test_parse_mw_only_market_watch_frames():
    assert rlc_ws.parse_mw("V,,,20260610,136") is None       # server-time frame
    assert rlc_ws.parse_mw("N2,IRT3SORF0001,21012,20260610113413,354665311") is None
    assert rlc_ws.parse_mw("not a frame") is None
    assert rlc_ws.parse_mw(123) is None                       # non-str
    parts = rlc_ws.parse_mw(_LIVE_MW)
    assert parts[0] == "MW" and parts[2] == "IRT3SORF0001"


# ---------------------------------------------------------------------------
# extract_buy_queue — the bl1 depth blob
# ---------------------------------------------------------------------------

def test_extract_buy_queue_from_live_frame():
    parts = rlc_ws.parse_mw(_LIVE_MW)
    assert rlc_ws.extract_buy_queue(parts) == 31706729


def test_extract_buy_queue_zero_is_zero_not_hold():
    # An emptied buy queue IS the thinned-queue condition — must return 0.
    parts = "MW,1,IRO1X,name,bl1;0;0;0;09:00:00;0;0;0;09:00:00".split(",")
    assert rlc_ws.extract_buy_queue(parts) == 0


def test_extract_buy_queue_missing_bl1_holds():
    # No depth level at all → None (fail-safe HOLD, never a sell signal).
    parts = "MW,1,IRO1X,name,9930,9370,7922712,55".split(",")
    assert rlc_ws.extract_buy_queue(parts) is None


def test_extract_buy_queue_malformed_bl1_holds():
    assert rlc_ws.extract_buy_queue(["MW", "1", "IRO1X", "bl1;"]) is None
    assert rlc_ws.extract_buy_queue(["MW", "1", "IRO1X", "bl1;abc;1"]) is None
    assert rlc_ws.extract_buy_queue(["MW", "1", "IRO1X", "bl1"]) is None


def test_extract_buy_queue_ignores_deeper_levels():
    # bl2/bl3 alone (no bl1) must not be mistaken for the best level.
    parts = ["MW", "1", "IRO1X", "bl2;139338;20658;2;t;0;0;0;t"]
    assert rlc_ws.extract_buy_queue(parts) is None


# ---------------------------------------------------------------------------
# [runtime] overrides — exir domain + WS URL (DB-pushed, no image rebuild)
# ---------------------------------------------------------------------------

def test_exir_base_default_and_override(monkeypatch):
    import runtime_config
    runtime_config.reset_cache()
    assert rlc_ws._exir_base("khobregan") == "https://khobregan.exirbroker.com"
    monkeypatch.setattr(runtime_config, "_snapshot",
                        lambda: {"exir_domain": "exir2.example"})
    assert rlc_ws._exir_base("khobregan") == "https://khobregan.exir2.example"


def test_ws_url_default_and_override(monkeypatch):
    import runtime_config
    runtime_config.reset_cache()
    assert rlc_ws._ws_url("push103.irbroker.com", "TOK") == (
        "wss://push103.irbroker.com/v2/ws?encoding=text&authToken=TOK&device=web"
    )
    monkeypatch.setattr(runtime_config, "_snapshot",
                        lambda: {"rlc_ws_scheme": "ws", "rlc_ws_path": "/v3/stream?t={token}"})
    assert rlc_ws._ws_url("h:9", "TOK") == "ws://h:9/v3/stream?t=TOK"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
