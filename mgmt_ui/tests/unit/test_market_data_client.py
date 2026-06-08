"""Unit tests for the mgmt-side market-data sidecar client (#108/#109).

No network: ``httpx.AsyncClient`` and ``settings_store.get_setting`` are stubbed.
The contract under test is graceful degradation — every helper returns ``[]`` /
``None`` rather than raising when the sidecar is slow / down / 404s.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services import market_data_client as mdc


class _FakeResp:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        if self._exc is not None:
            raise self._exc
        return self._resp


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    async def _get_setting(_db, _key):
        return "http://market-data:8077"
    monkeypatch.setattr(mdc.settings_store, "get_setting", _get_setting)


def _patch_client(monkeypatch, resp=None, exc=None):
    monkeypatch.setattr(mdc.httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp, exc))


async def test_search_returns_instruments(monkeypatch):
    _patch_client(monkeypatch, _FakeResp({"instruments": [{"isin": "IRO1SROD0001", "symbol": "سرود", "name": "x"}]}))
    out = await mdc.search_instruments(MagicMock(), "سرو")
    assert out == [{"isin": "IRO1SROD0001", "symbol": "سرود", "name": "x"}]


async def test_search_short_query_skips_network(monkeypatch):
    # Too short → empty without any HTTP call (AsyncClient would raise if used).
    def _boom(*a, **k):
        raise AssertionError("should not hit the network")
    monkeypatch.setattr(mdc.httpx, "AsyncClient", _boom)
    assert await mdc.search_instruments(MagicMock(), "a") == []


async def test_search_swallows_errors(monkeypatch):
    _patch_client(monkeypatch, exc=RuntimeError("connection refused"))
    assert await mdc.search_instruments(MagicMock(), "foo") == []


async def test_last_price_ok(monkeypatch):
    _patch_client(monkeypatch, _FakeResp({"isin": "IRO1SROD0001", "last_price": 11150}))
    assert await mdc.get_last_price(MagicMock(), "IRO1SROD0001") == 11150


async def test_last_price_404_is_none(monkeypatch):
    _patch_client(monkeypatch, _FakeResp({}, status=404))
    assert await mdc.get_last_price(MagicMock(), "IRO1SROD0001") is None


async def test_last_price_error_is_none(monkeypatch):
    _patch_client(monkeypatch, exc=RuntimeError("timeout"))
    assert await mdc.get_last_price(MagicMock(), "IRO1SROD0001") is None


async def test_price_band_ok(monkeypatch):
    _patch_client(monkeypatch, _FakeResp({"ceiling": 9930, "floor": 9370}))
    assert await mdc.get_price_band(MagicMock(), "IRO1SROD0001") == (9930, 9370)


async def test_queue_ok(monkeypatch):
    _patch_client(monkeypatch, _FakeResp({"buy_volume": 100, "sell_volume": 50}))
    q = await mdc.get_queue(MagicMock(), "IRO1SROD0001")
    assert q["buy_volume"] == 100
