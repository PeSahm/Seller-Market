"""Tests for ``broker_client.get_orders`` — pagination + 401 refresh.

Mocks the httpx transport (same approach as ``test_broker_client``): captcha →
OCR → login → GetOrders. We assert the paginator walks every page and that a
401 mid-stream transparently re-logs-in and continues.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services import broker_client
from app.services.broker_client import get_orders


def _row(tracking):
    return {"trackingNumber": tracking, "isin": "IRO1PNES0001", "orderSide": 1, "state": 3}


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


@pytest.mark.asyncio
async def test_get_orders_paginates_all_pages(patch_httpx):
    """150 records over pageSize 100 → two GetOrders calls, 150 rows."""
    counters = {}

    def handler(request):
        canned = _auth_routes(request, counters)
        if canned is not None:
            return canned
        if "/api/v2/orders/GetOrders" in str(request.url):
            counters["orders"] = counters.get("orders", 0) + 1
            page = json.loads(request.content)["page"]
            if page == 1:
                rows = [_row(i) for i in range(100)]
            else:
                rows = [_row(i) for i in range(100, 150)]
            return httpx.Response(200, json={"rows": rows, "totalRecords": 150, "page": page})
        return httpx.Response(404, text=str(request.url))

    patch_httpx(handler)

    rows, err = await get_orders(
        broker_code="ayandeh", username="u", password="p",
        ocr_service_url="http://ocr.test",
        from_date="2025/11/01", to_date="2026/06/01", page_size=100,
    )
    assert err is None
    assert len(rows) == 150
    assert counters["orders"] == 2
    assert counters["login"] == 1


@pytest.mark.asyncio
async def test_get_orders_stops_on_empty_page(patch_httpx):
    """A page with no rows ends pagination even if totalRecords lies."""
    def handler(request):
        canned = _auth_routes(request, {})
        if canned is not None:
            return canned
        if "/api/v2/orders/GetOrders" in str(request.url):
            page = json.loads(request.content)["page"]
            rows = [_row(1)] if page == 1 else []
            return httpx.Response(200, json={"rows": rows, "totalRecords": 9999, "page": page})
        return httpx.Response(404)

    patch_httpx(handler)
    rows, err = await get_orders(
        broker_code="ayandeh", username="u", password="p",
        ocr_service_url="http://ocr.test",
        from_date="2025/11/01", to_date="2026/06/01", page_size=100,
    )
    assert err is None
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_get_orders_refreshes_on_401(patch_httpx):
    """A 401 on the first GetOrders drops the cached token, re-logs-in once,
    and retries the same page — caller still gets its rows."""
    counters = {"orders": 0}

    def handler(request):
        canned = _auth_routes(request, counters)
        if canned is not None:
            return canned
        if "/api/v2/orders/GetOrders" in str(request.url):
            counters["orders"] += 1
            if counters["orders"] == 1:
                return httpx.Response(401, json={"error": "expired"})
            return httpx.Response(200, json={"rows": [_row(1)], "totalRecords": 1, "page": 1})
        return httpx.Response(404)

    # Seed a stale token so the first GetOrders uses it and gets 401.
    broker_client._token_cache_put("ayandeh", "u", "p", "stale")
    patch_httpx(handler)

    rows, err = await get_orders(
        broker_code="ayandeh", username="u", password="p",
        ocr_service_url="http://ocr.test",
        from_date="2025/11/01", to_date="2026/06/01",
    )
    assert err is None
    assert len(rows) == 1
    assert counters["login"] == 1  # the refresh login
    assert counters["orders"] == 2  # 401 then success
