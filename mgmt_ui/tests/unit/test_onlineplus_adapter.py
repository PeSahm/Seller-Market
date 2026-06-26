"""Unit tests for the OnlinePlus (Hafez / Tadbir Online+) broker family:
the login classifier, the GetOrderList row mapper, registry routing, verify_isin
via the shared RLC backend, and the cookie-auth reads (get_orders / get_holdings).

All pure/sync or mocked — no DB, no network. The live wire shape these assert
against was confirmed by the Phase-0 read-only spike against Hafez (account
4580090306): cookie auth, ``MessageCode`` reject markers (``oms_1000`` /
``InvalidCaptcha``), and the ``Data.Result`` GetOrderList envelope.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import app.services.brokers._rlc as rlc_mod
import app.services.brokers.onlineplus as op
from app.services.brokers import registry
from app.services.brokers.base import CredStatus
from app.services.brokers.onlineplus import (
    OnlinePlusAdapter,
    _OnlinePlusInvalidCredentials,
)


# --------------------------------------------------------------------------
# login classifier
# --------------------------------------------------------------------------
def test_classify_login_oms_1000_is_invalid():
    assert op._classify_onlineplus_login(
        {"IsSuccessfull": False, "MessageCode": "oms_1000"}
    ) is True
    # case-insensitive
    assert op._classify_onlineplus_login(
        {"IsSuccessfull": False, "MessageCode": "OMS_1000"}
    ) is True


def test_classify_login_conservative():
    # success body never invalid (even if a stale code lingers)
    assert op._classify_onlineplus_login(
        {"IsSuccessfull": True, "MessageCode": "oms_1000"}
    ) is False
    # other failure markers are NOT a credential reject (→ transient/retry)
    assert op._classify_onlineplus_login(
        {"IsSuccessfull": False, "MessageCode": "OMS_2080"}
    ) is False
    assert op._classify_onlineplus_login(
        {"IsSuccessfull": False, "MessageCode": "InvalidCaptcha"}
    ) is False
    # junk / non-dict → never invalid
    assert op._classify_onlineplus_login({}) is False
    assert op._classify_onlineplus_login(None) is False
    assert op._classify_onlineplus_login("nope") is False


def test_is_invalid_captcha():
    assert op._is_invalid_captcha({"MessageCode": "InvalidCaptcha"}) is True
    assert op._is_invalid_captcha({"MessageCode": "invalidcaptcha"}) is True
    assert op._is_invalid_captcha({"MessageCode": "oms_1000"}) is False
    assert op._is_invalid_captcha(None) is False


# --------------------------------------------------------------------------
# cookie jar — F5 BIG-IP duplicate-name cookies (Hafez)
# --------------------------------------------------------------------------
def _f5_dup_cookies():
    """An httpx jar mimicking Hafez behind an F5 BIG-IP: the unique auth cookie
    + TWO same-name ``f5avr…_session_`` persistence cookies on different paths
    (the live shape that crashed verify with 'Multiple cookies exist')."""
    import httpx

    c = httpx.Cookies()
    c.set("AuthCookie_OnlineCookie", "auth-token",
          domain="api.hafezbroker.ir", path="/")
    c.set("f5avraaaaaaaaaaaaaaaa_session_", "v-root",
          domain="api.hafezbroker.ir", path="/")
    c.set("f5avraaaaaaaaaaaaaaaa_session_", "v-web",
          domain="api.hafezbroker.ir", path="/Web")
    return c


def test_dict_on_dup_httpx_cookies_raises_documents_bug():
    """Root cause: ``dict(httpx.Cookies)`` goes through ``.get(name)`` and
    raises CookieConflict on two cookies sharing a name."""
    import httpx

    with pytest.raises(httpx.CookieConflict):
        dict(_f5_dup_cookies())


def test_cookies_to_dict_survives_dup():
    """The fix flattens a dup-name jar without raising + keeps the auth cookie."""
    from app.services.brokers._cookies import cookies_to_dict

    out = cookies_to_dict(_f5_dup_cookies().jar)
    assert out["AuthCookie_OnlineCookie"] == "auth-token"
    assert "f5avraaaaaaaaaaaaaaaa_session_" in out


# --------------------------------------------------------------------------
# registry routing
# --------------------------------------------------------------------------
@pytest.fixture
def family_map():
    saved = registry._FAMILY_CACHE
    registry.set_family_map(
        {"gs": "ephoenix", "khobregan": "exir", "hafez": "onlineplus"}
    )
    try:
        yield
    finally:
        registry._FAMILY_CACHE = saved


def test_get_adapter_routes_onlineplus(family_map):
    a = registry.get_adapter("hafez")
    assert isinstance(a, OnlinePlusAdapter) and a.family == "onlineplus"
    assert a.code == "hafez"
    assert a._web_base == "https://online.hafezbroker.ir"


# --------------------------------------------------------------------------
# GetOrderList row mapping
# --------------------------------------------------------------------------
def _op_row():
    """A realistic filled-buy GetOrderList row (fields per the decompiled
    client + the live envelope)."""
    return {
        "OrderId": 987654,
        "OrderSide": "خرید",  # buy (Persian)
        "Symbol": "سرود",
        "SymbolIsin": "IRO1SROD0001",
        "Quantity": 600,
        "ExcutedAmount": 600,
        "OrderPrice": 9930,
        "OrderDate": "1405/03/12",   # Jalali == 2026-06-02
        "OrderTime": "13:27:08",
        "OrderState": "انجام شده",
    }


def test_map_onlineplus_row(family_map):
    from app.services.broker_orders import map_getorders_row

    customer = SimpleNamespace(
        id=uuid4(), agent_id=uuid4(), broker="hafez", username="4580090306"
    )
    out = map_getorders_row(_op_row(), customer)

    assert out["tracking_number"] == 987654
    assert out["broker_order_id"] == 987654
    assert out["isin"] == "IRO1SROD0001"
    assert out["symbol"] == "سرود"
    assert out["symbol_title"] is None
    assert out["order_side"] == 1  # خرید -> buy
    assert out["volume"] == 600
    assert out["executed_volume"] == 600
    assert out["price"] == Decimal("9930")
    assert out["state"] == 3
    assert out["is_done"] is True
    assert out["serial_number"] is None
    assert out["total_fee"] is None
    assert out["pam_code"] is None
    assert out["placed_at"] is not None
    assert (out["placed_at"].year, out["placed_at"].month) == (2026, 6)
    # UTC-labeled wall-clock (matches the ephoenix/exir mappers) so date-range
    # filters classify all families consistently near Tehran midnight.
    assert out["placed_at"].utcoffset() == timedelta(0)
    assert (out["placed_at"].hour, out["placed_at"].minute) == (13, 27)


def test_map_onlineplus_row_sell_side(family_map):
    from app.services.broker_orders import map_getorders_row

    customer = SimpleNamespace(id=uuid4(), agent_id=uuid4(), broker="hafez", username="x")
    row = _op_row()
    row["OrderSide"] = "فروش"  # sell
    out = map_getorders_row(row, customer)
    assert out["order_side"] == 2


def test_map_onlineplus_row_isin_casing_fallback(family_map):
    """RealtimePortfolio uses SymbolISIN; GetOrderList uses SymbolIsin — the
    mapper must accept either."""
    from app.services.broker_orders import _map_onlineplus_row

    customer = SimpleNamespace(id=uuid4(), agent_id=uuid4(), broker="hafez", username="x")
    row = _op_row()
    del row["SymbolIsin"]
    row["SymbolISIN"] = "IRO1FOLD0001"
    out = _map_onlineplus_row(row, customer)
    assert out["isin"] == "IRO1FOLD0001"


def test_map_onlineplus_and_ephoenix_same_keyset(family_map):
    """All family mappers must populate the identical column set."""
    from app.services.broker_orders import _map_ephoenix_row, _map_onlineplus_row

    customer = SimpleNamespace(id=uuid4(), agent_id=uuid4(), broker="hafez", username="x")
    assert set(_map_onlineplus_row(_op_row(), customer).keys()) == set(
        _map_ephoenix_row({}, customer).keys()
    )


def test_map_onlineplus_row_rejects_nonfinite_price(family_map):
    from app.services.broker_orders import _map_onlineplus_row

    customer = SimpleNamespace(id=uuid4(), agent_id=uuid4(), broker="hafez", username="x")
    row = _op_row()
    row["OrderPrice"] = "NaN"
    assert _map_onlineplus_row(row, customer)["price"] is None


# --------------------------------------------------------------------------
# verify_isin via the shared public RLC backend
# --------------------------------------------------------------------------
async def test_onlineplus_verify_isin_ok(monkeypatch):
    row = {
        "nc": "IRO1SROD0001", "sf": "سرود", "cn": "سيمان شاهرود",
        "hap": "9930.00", "lap": "9370.00", "ltp": "9700", "mxqo": "50000",
    }
    monkeypatch.setattr(rlc_mod, "rlc_instrument", AsyncMock(return_value=row))
    info = await OnlinePlusAdapter("hafez").verify_isin(
        "u", "p", "IRO1SROD0001", "http://ocr"
    )
    assert info.ok is True
    assert info.symbol == "سرود"
    assert info.max_price == 9930.0
    assert info.min_price == 9370.0
    assert info.max_volume == 50000


async def test_onlineplus_verify_isin_unknown(monkeypatch):
    monkeypatch.setattr(rlc_mod, "rlc_instrument", AsyncMock(return_value=None))
    info = await OnlinePlusAdapter("hafez").verify_isin(
        "u", "p", "IRO1XXXX0001", "http://ocr"
    )
    assert info.ok is False
    assert "not found" in (info.error or "").lower()


async def test_onlineplus_verify_isin_unreachable(monkeypatch):
    monkeypatch.setattr(
        rlc_mod, "rlc_instrument", AsyncMock(side_effect=RuntimeError("boom"))
    )
    info = await OnlinePlusAdapter("hafez").verify_isin(
        "u", "p", "IRO1SROD0001", "http://ocr"
    )
    assert info.ok is False
    assert "could not reach" in (info.error or "").lower()


async def test_onlineplus_verify_isin_blank():
    info = await OnlinePlusAdapter("hafez").verify_isin("u", "p", "   ", "http://ocr")
    assert info.ok is False


# --------------------------------------------------------------------------
# verify_credentials (mock _login)
# --------------------------------------------------------------------------
async def test_onlineplus_verify_credentials_ok(monkeypatch):
    async def fake_login(self, client, u, p, o):
        return {
            "api": "https://api.hafezbroker.ir",
            "cookies": {},
            "customer_name": "مصطفی اسماعیلی",
            "bourse": "اسمـ50113",
            "otp_required": False,
            "must_change_password": False,
        }

    monkeypatch.setattr(OnlinePlusAdapter, "_login", fake_login)
    res = await OnlinePlusAdapter("hafez").verify_credentials("u", "p", "http://ocr")
    assert res.ok is True
    assert res.status == CredStatus.VALID
    assert res.full_name == "مصطفی اسماعیلی"
    assert res.bourse_code == "اسمـ50113"


async def test_onlineplus_verify_credentials_otp_note(monkeypatch):
    async def fake_login(self, client, u, p, o):
        return {
            "api": "https://api.hafezbroker.ir", "cookies": {},
            "customer_name": "X", "bourse": "Y",
            "otp_required": True, "must_change_password": False,
        }

    monkeypatch.setattr(OnlinePlusAdapter, "_login", fake_login)
    res = await OnlinePlusAdapter("hafez").verify_credentials("u", "p", "http://ocr")
    # Credentials are valid even with OTP — but the message flags it untradable.
    assert res.ok is True and res.status == CredStatus.VALID
    assert "otp" in (res.message or "").lower()


async def test_onlineplus_verify_credentials_invalid(monkeypatch):
    async def fake_login(self, client, u, p, o):
        raise _OnlinePlusInvalidCredentials("نام کاربری /رمز عبور نامعتبر می باشد.")

    monkeypatch.setattr(OnlinePlusAdapter, "_login", fake_login)
    res = await OnlinePlusAdapter("hafez").verify_credentials("u", "p", "http://ocr")
    assert res.ok is False
    assert res.status == CredStatus.INVALID_CREDENTIALS


async def test_onlineplus_verify_credentials_transient(monkeypatch):
    async def fake_login(self, client, u, p, o):
        raise RuntimeError("OCR down")

    monkeypatch.setattr(OnlinePlusAdapter, "_login", fake_login)
    res = await OnlinePlusAdapter("hafez").verify_credentials("u", "p", "http://ocr")
    assert res.ok is False
    assert res.status == CredStatus.TRANSIENT


# --------------------------------------------------------------------------
# get_orders / get_holdings (cookie-auth reads, mocked transport)
# --------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    """A stand-in for the cookie-auth read client — async-context, fixed resp."""

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        return self._resp

    async def get(self, url):
        return self._resp


def _mock_reads(monkeypatch, resp):
    async def fake_session(self, u, p, o):
        return {"api": "https://api.hafezbroker.ir", "cookies": {}}

    monkeypatch.setattr(OnlinePlusAdapter, "_session", fake_session)
    monkeypatch.setattr(OnlinePlusAdapter, "_read_client", lambda self, s: _FakeClient(resp))


async def test_onlineplus_get_orders_rejects_non_filled_status():
    rows, err = await OnlinePlusAdapter("hafez").get_orders(
        "u", "p", "http://ocr.invalid",
        from_date="2026/06/01", to_date="2026/06/02", include_status=[2],
    )
    assert rows == []
    assert err is not None and "status 3" in err


async def test_onlineplus_get_orders_rejects_side_filter():
    rows, err = await OnlinePlusAdapter("hafez").get_orders(
        "u", "p", "http://ocr.invalid",
        from_date="2026/06/01", to_date="2026/06/02", side=1,
    )
    assert rows == [] and err is not None and "side" in err


async def test_onlineplus_get_orders_keeps_only_executed(monkeypatch):
    payload = {"Data": {"TotalRecord": 3, "Result": [
        {"OrderId": 1, "SymbolIsin": "IRO1AAA00001", "ExcutedAmount": 100},
        {"OrderId": 2, "SymbolIsin": "IRO1BBB00001", "ExcutedAmount": 0},   # placed, never filled
        {"OrderId": 3, "SymbolIsin": "IRO1CCC00001"},                        # no executed field
    ]}}
    _mock_reads(monkeypatch, _FakeResp(200, payload))
    rows, err = await OnlinePlusAdapter("hafez").get_orders(
        "u", "p", "http://ocr", from_date="2026/06/01", to_date="2026/06/24",
        include_status=[3],
    )
    assert err is None
    assert [r["OrderId"] for r in rows] == [1]


async def test_onlineplus_get_orders_isin_filter(monkeypatch):
    payload = {"Data": {"TotalRecord": 2, "Result": [
        {"OrderId": 1, "SymbolIsin": "IRO1AAA00001", "ExcutedAmount": 100},
        {"OrderId": 4, "SymbolIsin": "IRO1DDD00001", "ExcutedAmount": 50},
    ]}}
    _mock_reads(monkeypatch, _FakeResp(200, payload))
    rows, err = await OnlinePlusAdapter("hafez").get_orders(
        "u", "p", "http://ocr", from_date="2026/06/01", to_date="2026/06/24",
        include_status=[3], isin="IRO1DDD00001",
    )
    assert err is None
    assert [r["OrderId"] for r in rows] == [4]


async def test_onlineplus_get_holdings(monkeypatch):
    payload = {"Data": [
        {"SymbolISIN": "IRO1AAA00001", "RemainQuantity": 0},
        {"SymbolISIN": "IRO1SROD0001", "RemainQuantity": 1500},
    ]}
    _mock_reads(monkeypatch, _FakeResp(200, payload))
    qty = await OnlinePlusAdapter("hafez").get_holdings(
        "u", "p", "IRO1SROD0001", ocr_service_url="http://ocr"
    )
    assert qty == 1500


async def test_onlineplus_get_holdings_absent_is_zero(monkeypatch):
    payload = {"Data": [{"SymbolISIN": "IRO1AAA00001", "RemainQuantity": 10}]}
    _mock_reads(monkeypatch, _FakeResp(200, payload))
    qty = await OnlinePlusAdapter("hafez").get_holdings(
        "u", "p", "IRO1ZZZ00001", ocr_service_url="http://ocr"
    )
    assert qty == 0
