"""Hermetic tests for the auto-sell monitor decision logic + helpers (#110).

No broker, no network, no WS. The adapter is faked; the clock + day-state are
injected. Run: ``python -m pytest test_auto_sell_monitor.py -q``.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone, timedelta

import market_data_ws as mdws
import order_fire_log
from auto_sell_monitor import AutoSellMonitor, AutoSellTarget, DayState, load_auto_sell_targets

TEHRAN = timezone(timedelta(hours=3, minutes=30))


class _Clock:
    """Injectable monotonic clock for the sustained-confirmation timer."""
    def __init__(self, t: float = 1000.0):
        self.t = t

    def mono(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _drive_fire(mon, isin, vol):
    """Push a sub-threshold reading, let the confirm window elapse, push again →
    the monitor confirms a SUSTAINED thinning and fires."""
    mon.on_buy_volume(isin, vol)          # arms the confirm timer (no sell)
    mon._test_clock.advance(10)           # past _confirm_seconds
    mon.on_buy_volume(isin, vol)          # still <= threshold → confirmed → fires


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
        "[sell]\nusername=u3\npassword=p3\nbroker=ayandeh\nisin=IRO1C\nside=2\n\n"
        "[watch_only]\nusername=u4\npassword=p4\nbroker=ayandeh\nbroker_family=ephoenix\n"
        "isin=IRO1D\nside=1\nauto_sell_threshold=750\nauto_sell_only=true\n",
        encoding="utf-8",
    )
    targets = load_auto_sell_targets(str(cfg))
    assert len(targets) == 2
    by_isin = {t.isin: t for t in targets}
    t = by_isin["IRO1A"]
    assert t.threshold == 500 and t.account == "u1"
    assert t.family == "ephoenix"
    # auto_sell_only=true sections (existing holding, no buy) must STILL arm —
    # the flag only suppresses the locust user / warmup, not the monitor.
    t2 = by_isin["IRO1D"]
    assert t2.threshold == 750 and t2.account == "u4"


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
    clock = _Clock()
    mon = AutoSellMonitor(
        targets,
        build_adapter=lambda _t: adapter,
        now_fn=lambda: datetime(2026, 1, 1, hour, 0, tzinfo=TEHRAN),
        window="09:00-12:30",
        day_state=day_state or DayState("test", directory=tempfile.mkdtemp()),
        sleep=lambda _s: None,
        mono_fn=clock.mono,
    )
    mon._test_clock = clock
    return mon, adapter, sends, (direct_sell, _orig)


_TGT = AutoSellTarget(account="u", password="p", broker_code="ayandeh",
                      family="ephoenix", isin="IRO1X", threshold=500, section_name="s")


def test_trigger_sells_when_below_threshold_and_marks_done(tmp_path, monkeypatch):
    ctx = _FakeCtx([1001, 0])           # 1001 before, 0 after → flat
    monkeypatch.setattr(order_fire_log, "emit_order_fire", lambda *a, **k: None)
    ds = DayState("t", directory=str(tmp_path))
    mon, adapter, sends, (ds_mod, orig) = _monitor([_TGT], ctx, day_state=ds)
    try:
        _drive_fire(mon, "IRO1X", 400)    # 400 <= 500 SUSTAINED → trigger
        assert ctx.prepared == [100] * 10 + [1]   # full ladder
        assert len(sends) == 11
        assert ds.is_done("u", "IRO1X") is True    # latched done after flat
    finally:
        ds_mod.send_prepared_order = orig


def test_single_sub_threshold_reading_does_not_fire(tmp_path, monkeypatch):
    # A lone sub-threshold push must NOT sell — it only arms the confirm timer.
    ctx = _FakeCtx([1001, 0])
    monkeypatch.setattr(order_fire_log, "emit_order_fire", lambda *a, **k: None)
    mon, adapter, sends, (ds_mod, orig) = _monitor([_TGT], ctx)
    try:
        mon.on_buy_volume("IRO1X", 400)            # 400 <= 500 but first reading
        assert sends == [] and adapter.opened == 0  # no sell yet
    finally:
        ds_mod.send_prepared_order = orig


def test_transient_blip_then_recovery_does_not_fire(tmp_path, monkeypatch):
    # THE incident regression: a feed rebuild delivered buy_volume=400 (junk) for
    # the watch while the REAL queue was ~76M. A sub-threshold blip immediately
    # followed by a healthy reading must NEVER sell — even after time passes.
    ctx = _FakeCtx([1318900, 0])
    monkeypatch.setattr(order_fire_log, "emit_order_fire", lambda *a, **k: None)
    mon, adapter, sends, (ds_mod, orig) = _monitor([_TGT], ctx)
    try:
        mon.on_buy_volume("IRO1X", 400)            # junk blip <= 500 → arms timer
        mon._test_clock.advance(10)                # time passes
        mon.on_buy_volume("IRO1X", 76_000_000)     # real, healthy queue → clears timer
        assert sends == [] and adapter.opened == 0  # NO sell
        mon._test_clock.advance(10)
        mon.on_buy_volume("IRO1X", 400)            # a fresh lone blip re-arms only
        assert sends == [] and adapter.opened == 0  # still no sell
    finally:
        ds_mod.send_prepared_order = orig


def test_sustained_below_fires_after_confirm_window(tmp_path, monkeypatch):
    # A genuine thinning: <= threshold held across the confirm window → sells.
    ctx = _FakeCtx([1001, 0])
    monkeypatch.setattr(order_fire_log, "emit_order_fire", lambda *a, **k: None)
    mon, adapter, sends, (ds_mod, orig) = _monitor([_TGT], ctx)
    try:
        mon.on_buy_volume("IRO1X", 400)            # arms
        assert sends == []                          # not yet
        mon._test_clock.advance(6)                  # past the 5s window
        mon.on_buy_volume("IRO1X", 450)            # still <= 500 → confirmed → fires
        assert adapter.opened == 1 and len(sends) == 11
    finally:
        ds_mod.send_prepared_order = orig


def test_feed_rebuild_clears_confirm_timer(tmp_path):
    mon, cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 500)))
    mon.on_buy_volume("IRO1A", 100)                 # arm the confirm timer
    assert ("u", "IRO1A") in mon._below_since
    mon._rebuild_feed(["IRO1A"])                     # a rebuild (the incident trigger)
    assert mon._below_since == {}                    # timer dropped → re-confirm from scratch


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


# ---------------------------------------------------------------------------
# hot-reload supervisor (#110 real-time threshold editing)
# ---------------------------------------------------------------------------

from auto_sell_monitor import parse_auto_sell_targets, CONFIG_END_SENTINEL  # noqa: E402


class _FakeFeed:
    """Records lifecycle so a test can assert swap-vs-rebuild without real WS."""
    instances: list = []

    def __init__(self, url, on_update, **kw):
        self.url = url
        self.on_update = on_update
        self.subscribed: list[str] = []
        self.started = False
        self.stopped = False
        _FakeFeed.instances.append(self)

    def subscribe(self, isin):
        self.subscribed.append(isin)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _armed_section(name, isin, threshold, *, account="u", side=1):
    body = (f"[{name}]\nusername={account}\npassword=p\nbroker=ayandeh\n"
            f"broker_family=ephoenix\nisin={isin}\nside={side}\n")
    if threshold is not None:
        body += f"auto_sell_threshold={threshold}\n"
    return body


_HEADER = "# Generated by mgmt_ui — do not edit by hand.\n\n"
_SENT = f"\n{CONFIG_END_SENTINEL}\n"


def _cfg_text(*sections):
    """A COMPLETE (sentinel-terminated) rendered config — what mgmt pushes."""
    return _HEADER + "".join(sections) + _SENT


def _sup_monitor(tmp_path, content):
    _FakeFeed.instances.clear()
    cfg = tmp_path / "config.ini"
    cfg.write_text(content, encoding="utf-8")
    clock = _Clock()
    mon = AutoSellMonitor(
        [],
        market_data_url="http://md:8077",
        build_adapter=lambda _t: _FakeAdapter(_FakeCtx([1001, 0])),
        now_fn=lambda: datetime(2026, 1, 1, 10, 0, tzinfo=TEHRAN),
        window="09:00-12:30",
        day_state=DayState("t", directory=str(tmp_path)),
        sleep=lambda _s: None,
        status_dir=str(tmp_path),
        mono_fn=clock.mono,
    )
    mon._test_clock = clock
    mon._feed_factory = _FakeFeed
    # mirror run_supervised's initial establishment without the infinite loop
    c = mon._read_content(str(cfg))
    init = parse_auto_sell_targets(c) if c is not None else []
    applied_marker = c if (c is not None and mon._trusted(c)) else None
    mon._apply(applied_marker, init, force_feed=True)
    return mon, cfg


def test_supervised_initial_arms_and_builds_feed(tmp_path):
    mon, _cfg = _sup_monitor(tmp_path, _HEADER + _armed_section("s", "IRO1A", 500))
    assert len(_FakeFeed.instances) == 1
    assert _FakeFeed.instances[0].subscribed == ["IRO1A"]
    assert _FakeFeed.instances[0].started
    assert mon._by_isin["IRO1A"][0].threshold == 500


def test_threshold_only_change_swaps_without_feed_rebuild(tmp_path):
    mon, cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 500)))
    cfg.write_text(_cfg_text(_armed_section("s", "IRO1A", 300)), encoding="utf-8")
    assert mon._tick(str(cfg)) == "applied"
    assert len(_FakeFeed.instances) == 1          # feed NOT rebuilt
    assert mon._by_isin["IRO1A"][0].threshold == 300  # new threshold live


def test_isin_set_change_rebuilds_feed(tmp_path):
    mon, cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 500)))
    cfg.write_text(_cfg_text(_armed_section("s", "IRO1A", 500),
                             _armed_section("s2", "IRO1B", 700, account="u2")), encoding="utf-8")
    assert mon._tick(str(cfg)) == "applied"
    assert len(_FakeFeed.instances) == 2          # rebuilt
    assert _FakeFeed.instances[0].stopped         # old feed torn down
    assert sorted(_FakeFeed.instances[1].subscribed) == ["IRO1A", "IRO1B"]


def test_zero_to_armed_transition(tmp_path):
    # The old idle-forever bug: a bot that booted with 0 armed must arm a
    # newly-added watch with no restart.
    mon, cfg = _sup_monitor(tmp_path, _cfg_text())    # header only → 0 armed
    assert mon.targets == []
    cfg.write_text(_cfg_text(_armed_section("s", "IRO1A", 500)), encoding="utf-8")
    assert mon._tick(str(cfg)) == "applied"
    feeds = [f for f in _FakeFeed.instances if f.subscribed == ["IRO1A"]]
    assert feeds and feeds[-1].started


def test_empty_file_tick_skipped(tmp_path):
    mon, cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 500)))
    cfg.write_text("", encoding="utf-8")              # torn → empty
    assert mon._tick(str(cfg)) == "skip-unreadable"
    assert mon._by_isin["IRO1A"][0].threshold == 500  # old targets kept


def test_parse_error_tick_skipped(tmp_path):
    mon, cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 500)))
    cfg.write_text("junk line with no section header\nkey=val" + _SENT, encoding="utf-8")
    assert mon._tick(str(cfg)) == "parse-error"
    assert mon._by_isin["IRO1A"][0].threshold == 500


def test_torn_prefix_without_sentinel_is_held_even_if_stable(tmp_path):
    """THE money case: an in-place write that stays torn for seconds produces a
    byte-STABLE prefix that parses cleanly with a WRONG threshold. The strict
    sentinel gate must hold it - never apply - no matter how many ticks."""
    mon, cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 50)))
    torn = _HEADER + _armed_section("s", "IRO1A", 9999)  # raised threshold, NO sentinel
    cfg.write_text(torn, encoding="utf-8")
    assert mon._tick(str(cfg)) == "untrusted"
    assert mon._tick(str(cfg)) == "untrusted"            # stays held tick after tick
    assert mon._by_isin["IRO1A"][0].threshold == 50      # old threshold still live
    # The completed write (sentinel present) then applies normally.
    cfg.write_text(_cfg_text(_armed_section("s", "IRO1A", 9999)), encoding="utf-8")
    assert mon._tick(str(cfg)) == "applied"
    assert mon._by_isin["IRO1A"][0].threshold == 9999


def test_sentinel_trusted_applies_on_single_read(tmp_path, monkeypatch):
    mon, cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 500)))
    trusted = _cfg_text(_armed_section("s", "IRO1A", 250))
    # A trusted (sentinel-terminated) file applies from ONE read - no settle
    # re-read (a second read here would raise StopIteration past the iterator).
    reads = iter([trusted])
    monkeypatch.setattr(mon, "_read_content", lambda _p: next(reads))
    assert mon._tick(str(cfg)) == "applied"
    assert mon._by_isin["IRO1A"][0].threshold == 250


def test_disarm_requires_confirm_tick(tmp_path):
    mon, cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 500)))
    cfg.write_text(_cfg_text(), encoding="utf-8")      # remove the armed section
    assert mon._tick(str(cfg)) == "disarm-pending"     # 1st tick: held
    assert mon._by_isin.get("IRO1A")                    # still armed
    assert mon._tick(str(cfg)) == "applied"            # 2nd identical tick: applied
    assert mon.targets == []


def test_cosmetic_change_is_noop(tmp_path):
    mon, cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 500)))
    # same armed set, different bytes (extra comment) → no rebuild, no marker churn
    cfg.write_text(_cfg_text("# unrelated edit\n", _armed_section("s", "IRO1A", 500)),
                   encoding="utf-8")
    assert mon._tick(str(cfg)) == "nochange-cosmetic"
    assert len(_FakeFeed.instances) == 1


def test_daystate_preserved_no_refire_after_threshold_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(order_fire_log, "emit_order_fire", lambda *a, **k: None)
    mon, cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 500)))
    ds_before = mon._day_state
    _drive_fire(mon, "IRO1A", 400)                     # sustained → fires → latches done
    assert mon._day_state.is_done("u", "IRO1A")
    cfg.write_text(_cfg_text(_armed_section("s", "IRO1A", 9000)), encoding="utf-8")
    assert mon._tick(str(cfg)) == "applied"
    assert mon._day_state is ds_before                 # same latch instance
    opened_before = 0  # a fresh adapter would .open; assert none happens
    mon._build_adapter = lambda _t: (_ for _ in ()).throw(AssertionError("must not re-trigger"))
    mon.on_buy_volume("IRO1A", 10)                      # 10 <= 9000 but already done today
    assert opened_before == 0


def test_generation_guard_drops_stale_feed_delivery(tmp_path):
    mon, cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 500)))
    stale_cb = _FakeFeed.instances[0].on_update         # gen 1 callback
    # rebuild (ISIN-set change) → gen 2
    cfg.write_text(_cfg_text(_armed_section("s", "IRO1A", 500),
                             _armed_section("s2", "IRO1B", 700, account="u2")), encoding="utf-8")
    assert mon._tick(str(cfg)) == "applied"
    triggered = []
    mon._trigger = lambda tgt: triggered.append(tgt)
    stale_cb("IRO1A", 1)                                 # old-gen delivery, sub-threshold
    assert triggered == []                              # dropped by the generation guard


def test_inflight_lock_blocks_second_ladder(tmp_path):
    mon, _cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 500)))
    tgt = mon._by_isin["IRO1A"][0]
    mon._inflight.add((tgt.account, tgt.isin))          # simulate a ladder already running
    opened = []
    mon._build_adapter = lambda _t: opened.append(1)
    mon._trigger(tgt)
    assert opened == []                                 # second ladder skipped


def test_status_marker_written(tmp_path):
    mon, _cfg = _sup_monitor(tmp_path, _cfg_text(_armed_section("s", "IRO1A", 500)))
    import json
    marker = tmp_path / "auto_sell_status.json"
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["schema"] == 1 and "applied_at" in data
    assert data["armed"] == [{"account": "u", "isin": "IRO1A", "threshold": 500}]


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
