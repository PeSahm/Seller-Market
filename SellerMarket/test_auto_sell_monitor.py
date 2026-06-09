"""Hermetic tests for the auto-sell monitor decision logic + helpers (#110).

No broker, no network, no WS. The adapter is faked; the clock + day-state are
injected. Run: ``python -m pytest test_auto_sell_monitor.py -q``.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone, timedelta

import auto_sell_monitor as m
import market_data_ws as mdws
import order_fire_log
from auto_sell_monitor import AutoSellMonitor, AutoSellTarget, DayState, load_auto_sell_targets

TEHRAN = timezone(timedelta(hours=3, minutes=30))


# ---------------------------------------------------------------------------
# helpers: ws_base / parse_buy_volume / fire-log
# ---------------------------------------------------------------------------

def test_ws_base_conversion():
    assert mdws.ws_base("http://5.10.248.55:8077") == "ws://5.10.248.55:8077"
    assert mdws.ws_base("https://h:9/") == "wss://h:9"
    assert mdws.ws_base("ws://h:9") == "ws://h:9"


def test_parse_buy_volume():
    assert mdws.parse_buy_volume('{"isin":"X","buy_volume":1234}') == 1234
    assert mdws.parse_buy_volume('{"isin":"X"}') is None       # absent
    assert mdws.parse_buy_volume("not json") is None
    assert mdws.parse_buy_volume('{"buy_volume":null}') is None


def test_emit_order_fire_writes_side2_record(tmp_path):
    order_fire_log.emit_order_fire("u", "ayandeh", "IRO1X", 2,
                                   order_response="{}", run_results_dir=str(tmp_path))
    import json
    files = list(tmp_path.glob("order_fires_*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert rec["side"] == 2 and rec["isin"] == "IRO1X" and rec["schema_version"] == 1
    assert "fire_uid" in rec and rec["broker_code"] == "ayandeh"


# ---------------------------------------------------------------------------
# config parsing
# ---------------------------------------------------------------------------

def test_load_targets_keeps_only_armed(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        "[buy_armed]\nusername=u1\npassword=p1\nbroker=ayandeh\nbroker_family=ephoenix\n"
        "isin=IRO1A\nside=1\nauto_sell_threshold=500\n\n"
        "[buy_unarmed]\nusername=u2\npassword=p2\nbroker=ayandeh\nisin=IRO1B\nside=1\n\n"
        "[sell]\nusername=u3\npassword=p3\nbroker=ayandeh\nisin=IRO1C\nside=2\n",
        encoding="utf-8",
    )
    targets = load_auto_sell_targets(str(cfg))
    assert len(targets) == 1
    t = targets[0]
    assert t.isin == "IRO1A" and t.threshold == 500 and t.account == "u1"
    assert t.family == "ephoenix"


# ---------------------------------------------------------------------------
# DayState idempotency
# ---------------------------------------------------------------------------

def test_day_state_persists_and_reloads(tmp_path):
    ds = DayState("20260101", directory=str(tmp_path))
    assert ds.is_done("u", "IRO1X") is False
    ds.mark_done("u", "IRO1X")
    assert ds.is_done("u", "IRO1X") is True
    # A fresh DayState for the same day reloads the latch (restart safety).
    ds2 = DayState("20260101", directory=str(tmp_path))
    assert ds2.is_done("u", "IRO1X") is True


# ---------------------------------------------------------------------------
# on_buy_volume gating + trigger
# ---------------------------------------------------------------------------

class _FakeCtx:
    def __init__(self, holdings_seq):
        self._seq = list(holdings_seq)
        self.floor_price = 5
        self.max_order_volume = 100
        self.prepared = []

    def fetch_holdings(self):
        return self._seq.pop(0) if self._seq else 0

    def prepare_chunk(self, volume):
        self.prepared.append(volume)
        return ("prepared", volume)


class _FakeAdapter:
    def __init__(self, ctx):
        self._ctx = ctx
        self.opened = 0

    def open_sell_context(self, *, isin, config_section):
        self.opened += 1
        return self._ctx


def _monitor(targets, ctx, *, hour=10, day_state=None, send_status=200):
    sends = []

    def fake_send(prepared, **kw):
        sends.append(prepared)
        return send_status, b"ok"

    # Patch the direct sender the monitor calls.
    import direct_sell
    _orig = direct_sell.send_prepared_order
    direct_sell.send_prepared_order = fake_send

    adapter = _FakeAdapter(ctx)
    mon = AutoSellMonitor(
        targets,
        build_adapter=lambda _t: adapter,
        now_fn=lambda: datetime(2026, 1, 1, hour, 0, tzinfo=TEHRAN),
        window="09:00-12:30",
        day_state=day_state or DayState("test", directory=tempfile.mkdtemp()),
        sleep=lambda _s: None,
    )
    return mon, adapter, sends, (direct_sell, _orig)


_TGT = AutoSellTarget(account="u", password="p", broker_code="ayandeh",
                      family="ephoenix", isin="IRO1X", threshold=500, section_name="s")


def test_trigger_sells_when_below_threshold_and_marks_done(tmp_path, monkeypatch):
    ctx = _FakeCtx([1001, 0])           # 1001 before, 0 after → flat
    monkeypatch.setattr(order_fire_log, "emit_order_fire", lambda *a, **k: None)
    ds = DayState("t", directory=str(tmp_path))
    mon, adapter, sends, (ds_mod, orig) = _monitor([_TGT], ctx, day_state=ds)
    try:
        mon.on_buy_volume("IRO1X", 400)   # 400 <= 500 → trigger
        assert ctx.prepared == [100] * 10 + [1]   # full ladder
        assert len(sends) == 11
        assert ds.is_done("u", "IRO1X") is True    # latched done after flat
    finally:
        ds_mod.send_prepared_order = orig


def test_no_trigger_above_threshold(tmp_path, monkeypatch):
    ctx = _FakeCtx([1001, 0])
    monkeypatch.setattr(order_fire_log, "emit_order_fire", lambda *a, **k: None)
    mon, adapter, sends, (ds_mod, orig) = _monitor([_TGT], ctx)
    try:
        mon.on_buy_volume("IRO1X", 600)   # 600 > 500 → no sell
        assert sends == [] and adapter.opened == 0
    finally:
        ds_mod.send_prepared_order = orig


def test_none_buy_volume_holds(tmp_path, monkeypatch):
    ctx = _FakeCtx([1001, 0])
    monkeypatch.setattr(order_fire_log, "emit_order_fire", lambda *a, **k: None)
    mon, adapter, sends, (ds_mod, orig) = _monitor([_TGT], ctx)
    try:
        mon.on_buy_volume("IRO1X", None)  # dead feed → HOLD
        assert sends == [] and adapter.opened == 0
    finally:
        ds_mod.send_prepared_order = orig


def test_no_trigger_outside_market_hours(tmp_path, monkeypatch):
    ctx = _FakeCtx([1001, 0])
    monkeypatch.setattr(order_fire_log, "emit_order_fire", lambda *a, **k: None)
    mon, adapter, sends, (ds_mod, orig) = _monitor([_TGT], ctx, hour=14)  # after 12:30
    try:
        mon.on_buy_volume("IRO1X", 100)
        assert sends == [] and adapter.opened == 0
    finally:
        ds_mod.send_prepared_order = orig


def test_already_done_skips(tmp_path, monkeypatch):
    ctx = _FakeCtx([1001, 0])
    monkeypatch.setattr(order_fire_log, "emit_order_fire", lambda *a, **k: None)
    ds = DayState("t2", directory=str(tmp_path))
    ds.mark_done("u", "IRO1X")
    mon, adapter, sends, (ds_mod, orig) = _monitor([_TGT], ctx, day_state=ds)
    try:
        mon.on_buy_volume("IRO1X", 100)
        assert sends == [] and adapter.opened == 0   # already sold today
    finally:
        ds_mod.send_prepared_order = orig


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
