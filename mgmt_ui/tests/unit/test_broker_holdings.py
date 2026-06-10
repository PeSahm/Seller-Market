"""Tests for the holdings probe behind the "Auto-sell only" form preview.

``broker_client._ephoenix_get_holdings`` is exercised with a mocked httpx
transport (same approach as ``test_get_orders``): captcha → OCR → login →
portfolio. ``ExirAdapter.get_holdings`` is exercised with the adapter's
session/signed-get plumbing mocked, so no live login happens. The ``side=3``
form alias itself (``map_side_form``) is covered in the schema tests — not
re-tested here.
"""
from __future__ import annotations

import httpx
import pytest

from app.services import broker_client
from app.services.broker_client import _endpoints_for, _ephoenix_get_holdings
from app.services.brokers.exir import ExirAdapter


@pytest.fixture(autouse=True)
def _clear_token_cache():
    broker_client._TOKEN_CACHE.clear()
    yield
    broker_client._TOKEN_CACHE.clear()


@pytest.fixture
def patch_httpx(monkeypatch):
    state = {"handler": None, "counters": {}}
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(state["handler"]))
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    def configure(handler):
        state["handler"] = handler
        return state

    return configure


def _auth_routes(request, counters):
    """Return a canned response for the captcha/ocr/login leg, else None."""
    url = str(request.url)
    if "/api/Captcha/GetCaptcha" in url:
        return httpx.Response(200, json={
            "captchaByteData": "iVBORw0KGgo=", "salt": "s", "hashedCaptcha": "h",
        })
    if "/ocr/captcha-easy-base64" in url:
        return httpx.Response(200, text="ABCD")
    if "/api/v2/accounts/login" in url:
        counters["login"] = counters.get("login", 0) + 1
        return httpx.Response(200, json={"token": "tok"})
    return None


# ---------------------------------------------------------------------------
# _endpoints_for grows a "portfolio" entry (both URL families)
# ---------------------------------------------------------------------------


def test_endpoints_portfolio_ephoenix():
    """ephoenix portfolio = the backofficeexternal host (mirrors broker_enum)."""
    assert _endpoints_for("ayandeh")["portfolio"] == (
        "https://backofficeexternal-ayandeh.ephoenix.ir"
        "/api/portfolio/getrealsecuritypositionbydate"
    )


def test_endpoints_portfolio_ib():
    """ib portfolio lives on the api8 shard, same as customer_info."""
    assert _endpoints_for("ib")["portfolio"] == (
        "https://api8.ibtrader.ir/api/portfolio/getrealsecuritypositionbydate"
    )


# ---------------------------------------------------------------------------
# _ephoenix_get_holdings parse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ephoenix_holdings_truncates_float_volume(patch_httpx):
    """remainVolume arrives as a float like 445608.000 — truncated to int."""
    counters = {}

    def handler(request):
        canned = _auth_routes(request, counters)
        if canned is not None:
            return canned
        if "getrealsecuritypositionbydate" in str(request.url):
            return httpx.Response(200, json={
                "isError": False,
                "result": [
                    {"isin": "IRO1PNES0001", "remainVolume": 12.0},
                    {"isin": "IRO1SROD0001", "remainVolume": 445608.000},
                ],
            })
        return httpx.Response(404, text=str(request.url))

    patch_httpx(handler)

    holdings = await _ephoenix_get_holdings(
        "ayandeh", "u", "p", "IRO1SROD0001", ocr_service_url="http://ocr.test"
    )
    assert holdings == 445608
    assert isinstance(holdings, int)


@pytest.mark.asyncio
async def test_ephoenix_holdings_isin_absent_returns_zero(patch_httpx):
    """The ISIN missing from the portfolio = the account holds 0 shares —
    a VALID answer, not an error."""
    def handler(request):
        canned = _auth_routes(request, {})
        if canned is not None:
            return canned
        if "getrealsecuritypositionbydate" in str(request.url):
            return httpx.Response(200, json={
                "isError": False,
                "result": [{"isin": "IRO1PNES0001", "remainVolume": 100.0}],
            })
        return httpx.Response(404)

    patch_httpx(handler)

    holdings = await _ephoenix_get_holdings(
        "ayandeh", "u", "p", "IRO1SROD0001", ocr_service_url="http://ocr.test"
    )
    assert holdings == 0


@pytest.mark.asyncio
async def test_ephoenix_holdings_iserror_raises_with_message(patch_httpx):
    """A broker-side isError surfaces the (Persian) message verbatim."""
    def handler(request):
        canned = _auth_routes(request, {})
        if canned is not None:
            return canned
        if "getrealsecuritypositionbydate" in str(request.url):
            return httpx.Response(200, json={
                "isError": True, "message": "خطای کارگزار", "result": None,
            })
        return httpx.Response(404)

    patch_httpx(handler)

    with pytest.raises(RuntimeError, match="خطای کارگزار"):
        await _ephoenix_get_holdings(
            "ayandeh", "u", "p", "IRO1SROD0001", ocr_service_url="http://ocr.test"
        )


@pytest.mark.asyncio
async def test_ephoenix_holdings_refreshes_on_401(patch_httpx):
    """A 401 on portfolio drops the cached token, re-logs-in once, retries —
    same recovery as GetOrders."""
    counters = {"portfolio": 0}

    def handler(request):
        canned = _auth_routes(request, counters)
        if canned is not None:
            return canned
        if "getrealsecuritypositionbydate" in str(request.url):
            counters["portfolio"] += 1
            if counters["portfolio"] == 1:
                return httpx.Response(401, json={"error": "expired"})
            return httpx.Response(200, json={
                "isError": False,
                "result": [{"isin": "IRO1SROD0001", "remainVolume": 601.0}],
            })
        return httpx.Response(404)

    # Seed a stale token so the first portfolio call uses it and gets 401.
    broker_client._token_cache_put("ayandeh", "u", "p", "stale")
    patch_httpx(handler)

    holdings = await _ephoenix_get_holdings(
        "ayandeh", "u", "p", "IRO1SROD0001", ocr_service_url="http://ocr.test"
    )
    assert holdings == 601
    assert counters["login"] == 1  # the refresh login
    assert counters["portfolio"] == 2  # 401 then success


# ---------------------------------------------------------------------------
# ExirAdapter.get_holdings parse (session + signed-get mocked)
# ---------------------------------------------------------------------------


def _mock_exir_session(monkeypatch, payload, status_code=200):
    """Stub the adapter's login + signed read so get_holdings parses
    ``payload`` without any live captcha/login."""
    async def fake_session(self, username, password, ocr_service_url):
        return {"nt": "NT", "cookies": {}}

    async def fake_signed_get(self, client, nt, path):
        assert path == "/api/v1/user/portfoReport"
        return httpx.Response(status_code, json=payload)

    monkeypatch.setattr(ExirAdapter, "_session", fake_session)
    monkeypatch.setattr(ExirAdapter, "_signed_get", fake_signed_get)


@pytest.mark.asyncio
async def test_exir_holdings_matches_insmaxlcode(monkeypatch):
    """Rows are matched on insMaxLcode (== ISIN) and read the ``asset``
    quantity field — same as the bot adapter's _holdings."""
    _mock_exir_session(monkeypatch, {
        "result": [
            {"insMaxLcode": "IRO1PNES0001", "asset": 7},
            {"insMaxLcode": "IRO1SROD0001", "asset": 1001},
        ],
    })
    holdings = await ExirAdapter("khobregan").get_holdings(
        "u", "p", "IRO1SROD0001", ocr_service_url="http://ocr.test"
    )
    assert holdings == 1001


@pytest.mark.asyncio
async def test_exir_holdings_falls_back_to_remainqty(monkeypatch):
    """``asset`` missing → fall back to ``remainQty`` (bot-adapter parity)."""
    _mock_exir_session(monkeypatch, {
        "result": [{"insMaxLcode": "IRO1SROD0001", "remainQty": 42}],
    })
    holdings = await ExirAdapter("khobregan").get_holdings(
        "u", "p", "IRO1SROD0001", ocr_service_url="http://ocr.test"
    )
    assert holdings == 42


@pytest.mark.asyncio
async def test_exir_holdings_isin_absent_returns_zero(monkeypatch):
    _mock_exir_session(monkeypatch, {
        "result": [{"insMaxLcode": "IRO1PNES0001", "asset": 7}],
    })
    holdings = await ExirAdapter("khobregan").get_holdings(
        "u", "p", "IRO1SROD0001", ocr_service_url="http://ocr.test"
    )
    assert holdings == 0


@pytest.mark.asyncio
async def test_exir_holdings_raises_after_retry_on_non_200(monkeypatch):
    """A persistent non-200 drops the session, retries once, then raises —
    the dispatcher contract is raise-on-failure."""
    calls = {"session": 0, "invalidate": 0}

    async def fake_session(self, username, password, ocr_service_url):
        calls["session"] += 1
        return {"nt": "NT", "cookies": {}}

    async def fake_signed_get(self, client, nt, path):
        return httpx.Response(500, json={"type": "error"})

    def fake_invalidate(self, username, password):
        calls["invalidate"] += 1

    monkeypatch.setattr(ExirAdapter, "_session", fake_session)
    monkeypatch.setattr(ExirAdapter, "_signed_get", fake_signed_get)
    monkeypatch.setattr(ExirAdapter, "_invalidate_session", fake_invalidate)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await ExirAdapter("khobregan").get_holdings(
            "u", "p", "IRO1SROD0001", ocr_service_url="http://ocr.test"
        )
    assert calls["session"] == 2  # one retry with a fresh login
    assert calls["invalidate"] == 2
