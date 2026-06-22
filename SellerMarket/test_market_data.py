"""Hermetic tests for the market-data sidecar (rlc_market + the Flask app).

No network: the shared RLC session's ``get`` is monkeypatched. The Flask app
tests stub the rlc_market functions so they exercise routing/serialization only.
"""
from __future__ import annotations

import json

import pytest

import rlc_market
import rlc_price


class _Resp:
    def __init__(self, payload, status=200):
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _clear():
    rlc_market.clear_cache()
    rlc_price.clear_cache()
    yield
    rlc_market.clear_cache()
    rlc_price.clear_cache()


def _patch(monkeypatch, payload, capture=None):
    def fake_get(url, timeout=None, **kw):
        if capture is not None:
            capture.append(url)
        return _Resp(payload)
    # rlc_market._session IS rlc_price._session — one patch covers both.
    monkeypatch.setattr(rlc_market._session, "get", fake_get)


# ---------------------------------------------------------------------------
# last price
# ---------------------------------------------------------------------------

def test_last_price_prefers_ltp(monkeypatch):
    _patch(monkeypatch, [{"nc": "IRO1SROD0001", "ltp": 9930.0, "cp": 9900.0, "pcp": 9650.0}])
    assert rlc_market.get_last_price("IRO1SROD0001") == 9930


def test_last_price_falls_back_to_cp_then_pcp(monkeypatch):
    _patch(monkeypatch, [{"nc": "IRO1SROD0001", "ltp": 0, "cp": 0, "pcp": 9650.0}])
    assert rlc_market.get_last_price("IRO1SROD0001") == 9650


def test_last_price_unknown_isin_is_zero(monkeypatch):
    _patch(monkeypatch, [{"nc": "OTHER", "ltp": 5}])
    assert rlc_market.get_last_price("IRO1SROD0001") == 0


def test_last_price_caches(monkeypatch):
    calls = []
    _patch(monkeypatch, [{"nc": "IRO1SROD0001", "ltp": 9930}], capture=calls)
    rlc_market.get_last_price("IRO1SROD0001")
    rlc_market.get_last_price("IRO1SROD0001")
    assert len(calls) == 1


def test_last_price_bad_payload_zero(monkeypatch):
    _patch(monkeypatch, {"not": "a list"})
    assert rlc_market.get_last_price("IRO1SROD0001") == 0


def test_market_host_runtime_override(monkeypatch):
    # The RLC market-data host is redirectable fleet-wide via [runtime].
    import runtime_config
    monkeypatch.setattr(runtime_config, "_snapshot",
                        lambda: {"rlc_market_host": "https://core2.example/"})
    calls = []
    _patch(monkeypatch, [{"nc": "IRO1SROD0001", "ltp": 9930}], capture=calls)
    rlc_market.get_last_price("IRO1SROD0001")
    assert calls and calls[0].startswith("https://core2.example//StockInformationHandler?")


# ---------------------------------------------------------------------------
# instruments + search
# ---------------------------------------------------------------------------

def test_instruments_dict_rows(monkeypatch):
    _patch(monkeypatch, [
        {"nc": "IRO1SROD0001", "cn": "سیمان شاهرود", "sf": "سرود"},
        {"nc": "IRO3SMBZ0001", "cn": "Foo Co", "sf": "FOOB"},
        {"no_isin": "skip"},
    ])
    rows = rlc_market.get_instruments(force=True)
    assert {r["isin"] for r in rows} == {"IRO1SROD0001", "IRO3SMBZ0001"}


def test_instruments_delimited_string_rows(monkeypatch):
    _patch(monkeypatch, ["سرود,IRO1SROD0001,سیمان شاهرود", "garbage,row,nope"])
    rows = rlc_market.get_instruments(force=True)
    assert any(r["isin"] == "IRO1SROD0001" for r in rows)


def test_instruments_bad_payload_returns_empty(monkeypatch):
    _patch(monkeypatch, {"not": "a list"})
    assert rlc_market.get_instruments(force=True) == []


def test_search_filters_ranks_and_caps(monkeypatch):
    _patch(monkeypatch, [
        {"nc": "IRO1SROD0001", "cn": "سیمان شاهرود", "sf": "سرود"},
        {"nc": "IRO3SMBZ0001", "cn": "Foo Co", "sf": "FOOB"},
    ])
    rlc_market.get_instruments(force=True)
    hits = rlc_market.search_instruments("foo")
    assert len(hits) == 1 and hits[0]["isin"] == "IRO3SMBZ0001"
    assert rlc_market.search_instruments("") == []


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------

def test_queue_from_getstockprice2_row(monkeypatch):
    # LIVE-CONFIRMED: best-level queue lives on the getstockprice2 row —
    # bbq/bsq (best buy/sell qty), nbb/nbs (order counts), bbp/bsp (best prices).
    _patch(monkeypatch, [{
        "nc": "IRO1SROD0001",
        "bbq": 12345, "bsq": 6789, "nbb": 3, "nbs": 2, "bbp": 11150, "bsp": 0,
    }])
    q = rlc_market.get_queue("IRO1SROD0001")
    assert q["buy_volume"] == 12345
    assert q["sell_volume"] == 6789
    assert q["buy_count"] == 3 and q["sell_count"] == 2
    assert q["best_buy_price"] == 11150


def test_queue_unknown_isin_returns_none(monkeypatch):
    _patch(monkeypatch, [{"nc": "OTHER", "bbq": 5}])
    assert rlc_market.get_queue("IRO1SROD0001") is None


def test_queue_empty_book_is_zero_not_none(monkeypatch):
    # A found row with no resting orders → an HONEST zero (auto-sell reads
    # buy_volume=0 as "empty buy queue"), NOT None ("no data").
    _patch(monkeypatch, [{"nc": "IRO1SROD0001", "bbq": 0, "bsq": 0}])
    q = rlc_market.get_queue("IRO1SROD0001")
    assert q is not None and q["buy_volume"] == 0


def test_queue_caches(monkeypatch):
    calls = []
    _patch(monkeypatch, [{"nc": "X", "bbq": 1}], capture=calls)
    rlc_market.get_queue("X")
    rlc_market.get_queue("X")
    assert len(calls) == 1


def test_queue_transport_error_returns_none(monkeypatch):
    def boom(url, timeout=None, **kw):
        raise rlc_price.requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(rlc_market._session, "get", boom)
    assert rlc_market.get_queue("X") is None


# ---------------------------------------------------------------------------
# Flask app (routing / serialization only — rlc_market stubbed)
# ---------------------------------------------------------------------------

flask = pytest.importorskip("flask")  # noqa: F841 — skip app tests if flask absent


def test_app_health():
    from market_data_app import app
    r = app.test_client().get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_app_price_band(monkeypatch):
    import market_data_app
    monkeypatch.setattr(market_data_app.rlc_market, "get_price_band", lambda isin: (9930, 9370))
    r = market_data_app.app.test_client().get("/price-band?isin=IRO1SROD0001")
    assert r.status_code == 200
    assert r.get_json() == {"isin": "IRO1SROD0001", "ceiling": 9930, "floor": 9370}


def test_app_last_price_404(monkeypatch):
    import market_data_app
    monkeypatch.setattr(market_data_app.rlc_market, "get_last_price", lambda isin: 0)
    r = market_data_app.app.test_client().get("/last-price?isin=IRO1SROD0001")
    assert r.status_code == 404


def test_app_queue_ok(monkeypatch):
    import market_data_app
    monkeypatch.setattr(
        market_data_app.rlc_market, "get_queue",
        lambda isin: {"buy_volume": 10, "sell_volume": 5, "buy_count": 1, "sell_count": 1, "raw": {}},
    )
    r = market_data_app.app.test_client().get("/queue?isin=IRO1SROD0001")
    assert r.status_code == 200 and r.get_json()["buy_volume"] == 10


def test_app_search(monkeypatch):
    import market_data_app
    monkeypatch.setattr(
        market_data_app.rlc_market, "search_instruments",
        lambda q, limit=20: [{"isin": "IRO1SROD0001", "symbol": "سرود", "name": "x"}],
    )
    r = market_data_app.app.test_client().get("/search?q=sr")
    assert r.status_code == 200 and r.get_json()["count"] == 1


def test_app_missing_isin_400():
    from market_data_app import app
    assert app.test_client().get("/price-band").status_code == 400
