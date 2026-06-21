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


async def test_instruments_ok(monkeypatch):
    _patch_client(monkeypatch, _FakeResp(
        {"instruments": [{"isin": "IRO3SMBZ0001", "symbol": "سرود", "name": "سیمان شاهرود"}]}
    ))
    out = await mdc.get_instruments(MagicMock())
    assert out == [{"isin": "IRO3SMBZ0001", "symbol": "سرود", "name": "سیمان شاهرود"}]


async def test_instruments_swallows_errors(monkeypatch):
    _patch_client(monkeypatch, exc=RuntimeError("sidecar down"))
    assert await mdc.get_instruments(MagicMock()) == []


# ---------------------------------------------------------------------------
# Failover pool (HA): comma-separated market_data_url, prefer-primary failover
# ---------------------------------------------------------------------------


class _RouteClient:
    """AsyncClient stand-in routing by URL substring → ``_FakeResp`` or raises."""

    calls: list[str] = []

    def __init__(self, routes):
        self._routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        _RouteClient.calls.append(url)
        for sub, val in self._routes.items():
            if sub in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError(f"no route for {url}")


def _patch_routed(monkeypatch, routes, setting):
    _RouteClient.calls = []

    async def _gs(_db, _k):
        return setting

    monkeypatch.setattr(mdc.settings_store, "get_setting", _gs)
    monkeypatch.setattr(mdc.httpx, "AsyncClient", lambda *a, **k: _RouteClient(routes))


def test_parse_bases():
    assert mdc._parse_bases("http://a:8077") == ["http://a:8077"]
    assert mdc._parse_bases("http://a:8077, http://b:8077/") == ["http://a:8077", "http://b:8077"]
    assert mdc._parse_bases("") == [mdc._DEFAULT_BASE]
    assert mdc._parse_bases(None) == [mdc._DEFAULT_BASE]


async def test_search_fails_over_to_backup(monkeypatch):
    _patch_routed(
        monkeypatch,
        {
            "primary:8077": mdc.httpx.ConnectError("primary down"),
            "backup:8077": _FakeResp({"instruments": [{"isin": "X"}]}),
        },
        "http://primary:8077, http://backup:8077",
    )
    out = await mdc.search_instruments(MagicMock(), "foo")
    assert out == [{"isin": "X"}]
    # prefer-primary: the primary is tried BEFORE the backup (order, not presence)
    primary_i = next(i for i, u in enumerate(_RouteClient.calls) if "primary:8077" in u)
    backup_i = next(i for i, u in enumerate(_RouteClient.calls) if "backup:8077" in u)
    assert primary_i < backup_i


async def test_all_bases_fail_is_graceful(monkeypatch):
    _patch_routed(
        monkeypatch,
        {
            "a:8077": mdc.httpx.ConnectError("down"),
            "b:8077": mdc.httpx.ConnectError("down"),
        },
        "http://a:8077, http://b:8077",
    )
    assert await mdc.search_instruments(MagicMock(), "foo") == []
    assert await mdc.get_last_price(MagicMock(), "X") is None
    assert await mdc.get_price_band(MagicMock(), "X") is None
    assert await mdc.get_queue(MagicMock(), "X") is None
    assert await mdc.get_instruments(MagicMock()) == []


async def test_404_on_primary_does_not_fail_over(monkeypatch):
    # A healthy sidecar's 404 ("no data for this ISIN") is definitive — the next
    # base shares the same RLC source. Failing over would be pointless, and (if a
    # backup ever DID answer) wrong. So a 404 returns None without touching backup.
    _patch_routed(
        monkeypatch,
        {
            "primary:8077": _FakeResp({}, status=404),
            "backup:8077": _FakeResp({"last_price": 999}),
        },
        "http://primary:8077, http://backup:8077",
    )
    assert await mdc.get_last_price(MagicMock(), "X") is None
    assert all("backup:8077" not in u for u in _RouteClient.calls)
