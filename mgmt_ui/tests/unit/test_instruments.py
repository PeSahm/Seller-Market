"""Unit tests for the ISIN→symbol resolver cache + renderer (symbol-name feature).

No network / no DB engine: the sidecar client (`market_data_client.get_instruments`)
is monkeypatched and the AsyncSession is a stub whose `execute(...).all()` returns
the `broker_orders` supplement rows. The contracts under test:

* graceful degradation — `lookup` returns ``None`` (never raises) on a cold or
  unknown cache; a sidecar/DB failure keeps the previous map.
* the sidecar wins over the `broker_orders` supplement on overlap.
* the renderer escapes HTML and renders "symbol + muted ISIN" / bare ISIN.
"""
from __future__ import annotations

import pytest
from markupsafe import Markup

from app.services import instruments
from app.services import market_data_client as mdc


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """Stands in for an AsyncSession: ``execute(select(...)).all()`` -> rows."""

    def __init__(self, broker_rows=None):
        self._rows = broker_rows or []

    async def execute(self, _stmt):
        return _Result(self._rows)


@pytest.fixture(autouse=True)
def _reset_cache():
    # The cache is module-global state (like the broker family cache); reset it
    # around every test so ordering can't leak a warm map.
    instruments._reset()
    yield
    instruments._reset()


def _patch_sidecar(monkeypatch, instruments_list=None, exc=None):
    async def _fake(_session):
        if exc is not None:
            raise exc
        return list(instruments_list or [])

    monkeypatch.setattr(mdc, "get_instruments", _fake)


# --------------------------------------------------------------------------- #
# warm / ensure / lookup
# --------------------------------------------------------------------------- #
async def test_warm_and_lookup_hit(monkeypatch):
    _patch_sidecar(monkeypatch, [{"isin": "IRO3SMBZ0001", "symbol": "سرود", "name": "سیمان شاهرود"}])
    await instruments.warm_instruments(_FakeDB())
    assert instruments.lookup("IRO3SMBZ0001") == {"symbol": "سرود", "name": "سیمان شاهرود"}


async def test_lookup_miss_is_none(monkeypatch):
    _patch_sidecar(monkeypatch, [{"isin": "IRO3SMBZ0001", "symbol": "سرود", "name": "x"}])
    await instruments.warm_instruments(_FakeDB())
    assert instruments.lookup("UNKNOWN0001") is None


def test_cold_cache_lookup_is_none():
    # never warmed → graceful None, no raise (template shows bare ISIN)
    assert instruments.lookup("ANYTHING") is None


def test_lookup_empty_isin_is_none():
    instruments.set_instruments_map({"AAA": {"symbol": "x", "name": "y"}})
    assert instruments.lookup("") is None


async def test_supplement_fills_gaps_and_sidecar_wins(monkeypatch):
    # sidecar has AAA; broker_orders has AAA (different) + BBB. AAA keeps the
    # sidecar value; BBB is added from the supplement.
    _patch_sidecar(monkeypatch, [{"isin": "AAA", "symbol": "side-A", "name": "n"}])
    rows = [("AAA", "bo-A", "bo-title-A"), ("BBB", "نماد", "شرکت")]
    await instruments.warm_instruments(_FakeDB(rows))
    assert instruments.lookup("AAA")["symbol"] == "side-A"          # sidecar wins
    assert instruments.lookup("BBB") == {"symbol": "نماد", "name": "شرکت"}


async def test_sidecar_down_yields_empty(monkeypatch):
    # get_instruments is itself graceful ([]); empty supplement → empty cache,
    # lookup None, no exception.
    _patch_sidecar(monkeypatch, [])
    await instruments.warm_instruments(_FakeDB([]))
    assert instruments.lookup("X") is None


async def test_warm_swallows_error_keeps_previous(monkeypatch):
    _patch_sidecar(monkeypatch, [{"isin": "AAA", "symbol": "س", "name": "n"}])
    await instruments.warm_instruments(_FakeDB())
    assert instruments.lookup("AAA") is not None

    class _BoomDB:
        async def execute(self, _stmt):
            raise RuntimeError("db down")

    await instruments.warm_instruments(_BoomDB())   # raises inside _load
    assert instruments.lookup("AAA") is not None    # previous map preserved


async def test_ensure_rewarms_only_when_stale(monkeypatch):
    calls = {"n": 0}

    async def _fake(_session):
        calls["n"] += 1
        return [{"isin": "AAA", "symbol": "س", "name": "n"}]

    monkeypatch.setattr(mdc, "get_instruments", _fake)
    clock = {"t": 1000.0}
    monkeypatch.setattr(instruments.time, "monotonic", lambda: clock["t"])

    await instruments.ensure_instruments(_FakeDB())   # cold → warm
    assert calls["n"] == 1
    await instruments.ensure_instruments(_FakeDB())   # fresh → no-op
    assert calls["n"] == 1
    clock["t"] += instruments._TTL_SECONDS + 1        # age past TTL
    await instruments.ensure_instruments(_FakeDB())   # stale → re-warm
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# renderer / inline resolver
# --------------------------------------------------------------------------- #
def test_render_with_caller_symbol():
    out = instruments.render_symbol_label("IRO3SMBZ0001", symbol="سرود")
    assert isinstance(out, Markup)
    s = str(out)
    assert 'dir="auto"' in s and "سرود" in s
    assert "IRO3SMBZ0001" in s and "text-muted" in s


def test_render_from_cache():
    instruments.set_instruments_map({"IRO3SMBZ0001": {"symbol": "سرود", "name": "x"}})
    s = str(instruments.render_symbol_label("IRO3SMBZ0001"))
    assert "سرود" in s and "IRO3SMBZ0001" in s


def test_render_unknown_is_bare_isin():
    s = str(instruments.render_symbol_label("UNKNOWN0001"))
    assert "UNKNOWN0001" in s
    assert 'dir="auto"' not in s and "text-muted" not in s


def test_render_caller_symbol_beats_cache():
    instruments.set_instruments_map({"AAA": {"symbol": "cache-sym", "name": "n"}})
    s = str(instruments.render_symbol_label("AAA", symbol="row-sym"))
    assert "row-sym" in s and "cache-sym" not in s


def test_render_escapes_html():
    s = str(instruments.render_symbol_label("<isin>", symbol="<b>x</b>"))
    assert "<script>" not in s and "<b>x</b>" not in s
    assert "&lt;b&gt;" in s and "&lt;isin&gt;" in s


def test_symbol_text_resolution_order():
    instruments.set_instruments_map(
        {"AAA": {"symbol": "cache", "name": "company"}, "BBB": {"symbol": "", "name": "OnlyName"}}
    )
    assert instruments.symbol_text("AAA", symbol="row") == "row"
    assert instruments.symbol_text("AAA", title="t") == "t"
    assert instruments.symbol_text("AAA") == "cache"
    assert instruments.symbol_text("BBB") == "OnlyName"   # empty symbol → name
    assert instruments.symbol_text("ZZZ") == ""           # unknown


# --------------------------------------------------------------------------- #
# template integration — the `symbol_label` global is wired into the real engine
# --------------------------------------------------------------------------- #
def test_auto_sell_rows_template_renders_symbol():
    from app.routers.dashboard import templates  # registers the globals on import

    instruments.set_instruments_map({"IRO3SMBZ0001": {"symbol": "سرود", "name": "x"}})

    def _row(isin):
        return {
            "customer": "Mostafa", "broker": "ayandeh", "isin": isin,
            "threshold": 1000, "buy_volume": None, "triggered": False,
            "fired_today": False, "sell_only": False, "applied": False,
            "applied_at": None,
        }

    html = templates.env.get_template("partials/auto_sell_rows.html").render(
        rows=[_row("IRO3SMBZ0001"), _row("UNKNOWN0001")]
    )
    assert "سرود" in html            # known ISIN → symbol shown
    assert "IRO3SMBZ0001" in html    # known ISIN → muted ISIN still present
    assert "UNKNOWN0001" in html     # unknown ISIN → bare ISIN (graceful)
