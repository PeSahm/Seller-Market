"""Hermetic tests for :mod:`rlc_price` — the broker-native RLC band client.

No network: the module session's ``get`` is monkeypatched. The sample payload
mirrors a real ``StockInformationHandler`` response (سرود / IRO1SROD0001).
"""
from __future__ import annotations

import json

import pytest

import rlc_price


# One real-shaped row (trimmed): hap = upper (BUY ceiling), lap = lower (SELL floor).
_ROW_SROD = {
    "nc": "IRO1SROD0001", "cn": "سیمان‌شاهرود", "sf": "سرود",
    "cp": 9930.0, "ltp": 9930.0, "pcp": 9650.0,
    "hap": 9930.0000000000000, "lap": 9370.0000000000000,
}
_ROW_SMBZ = {"nc": "IRO3SMBZ0001", "hap": 12340.0, "lap": 11200.0}


class _FakeResp:
    def __init__(self, payload, status=200):
        self.text = json.dumps(payload)
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _clear():
    rlc_price.clear_cache()
    yield
    rlc_price.clear_cache()


def _patch_get(monkeypatch, payload, capture=None):
    def fake_get(url, timeout=None, **kw):
        if capture is not None:
            capture.append(url)
        return _FakeResp(payload)
    monkeypatch.setattr(rlc_price._session, "get", fake_get)


# ---------------------------------------------------------------------------
# URL + parsing
# ---------------------------------------------------------------------------

def test_build_url_encodes_arr_and_handler():
    url = rlc_price._build_url(["IRO1SROD0001", "IRO3SMBZ0001"])
    assert url.startswith("https://core.tadbirrlc.com//StockInformationHandler?")
    assert "getstockprice2" in url
    # ISINs ride in the URL-encoded arr blob, comma-joined.
    assert "IRO1SROD0001%2CIRO3SMBZ0001" in url
    assert url.endswith("&jsoncallback=")


def test_parse_rows_maps_hap_lap_to_ceiling_floor():
    out = rlc_price._parse_rows([_ROW_SROD, _ROW_SMBZ])
    assert out["IRO1SROD0001"] == (9930, 9370)
    assert out["IRO3SMBZ0001"] == (12340, 11200)


def test_parse_rows_skips_malformed_and_zero():
    rows = [
        {"nc": "", "hap": 1, "lap": 1},            # no code
        {"nc": "A", "hap": None, "lap": 5},        # bad upper
        {"nc": "B", "hap": 0.0, "lap": 0.0},       # zero ceiling → dropped
        {"hap": 9, "lap": 8},                       # missing nc
        "not-a-dict",
        _ROW_SROD,
    ]
    out = rlc_price._parse_rows(rows)
    assert out == {"IRO1SROD0001": (9930, 9370)}


def test_parse_rows_non_list_is_empty():
    assert rlc_price._parse_rows({"nc": "X"}) == {}
    assert rlc_price._parse_rows(None) == {}


# ---------------------------------------------------------------------------
# get_price_band + caching
# ---------------------------------------------------------------------------

def test_get_price_band_returns_ceiling_floor(monkeypatch):
    _patch_get(monkeypatch, [_ROW_SROD])
    assert rlc_price.get_price_band("IRO1SROD0001") == (9930, 9370)


def test_get_price_band_caches_second_call(monkeypatch):
    calls = []
    _patch_get(monkeypatch, [_ROW_SROD], capture=calls)
    rlc_price.get_price_band("IRO1SROD0001")
    rlc_price.get_price_band("IRO1SROD0001")
    assert len(calls) == 1  # second call served from cache, no second fetch


def test_get_price_band_unknown_isin_raises(monkeypatch):
    _patch_get(monkeypatch, [_ROW_SROD])  # response lacks the requested ISIN
    with pytest.raises(ValueError, match="no price band"):
        rlc_price.get_price_band("IRO9ZZZZ0001")


def test_prefetch_warms_multiple(monkeypatch):
    calls = []
    _patch_get(monkeypatch, [_ROW_SROD, _ROW_SMBZ], capture=calls)
    rlc_price.prefetch(["IRO1SROD0001", "IRO3SMBZ0001"])
    # both now served from cache — no further fetches
    assert rlc_price.get_price_band("IRO1SROD0001") == (9930, 9370)
    assert rlc_price.get_price_band("IRO3SMBZ0001") == (12340, 11200)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Proxy-bypass: the Iranian RLC host must be reached directly.
# ---------------------------------------------------------------------------

def test_session_ignores_env_proxy():
    # trust_env=False → requests never routes this host through the VPS's
    # foreign HTTP proxy (which can't reach it).
    assert rlc_price._session.trust_env is False
