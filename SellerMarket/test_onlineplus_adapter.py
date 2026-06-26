"""Hermetic tests for the OnlinePlus (Hafez / Tadbir Online+) bot adapter (Phase 2).

No network: login (captcha→OCR→cookie), the cookie-auth reads (buying power +
holdings), and the RLC price band are all faked. Asserts the cookie-only order
shape (no Bearer, no signer), fee-adjusted BUY sizing, SELL-from-holdings, the
max-order-qty / max_volume caps, the invalid-creds + OTP skips, and the auto-sell
SellContext. The live wire shape these assert against was confirmed by the
Phase-0 read-only spike against Hafez (the operator's own account).

Run: ``python -m pytest test_onlineplus_adapter.py -q``.
"""
from __future__ import annotations

import json

import pytest
import requests

import onlineplus_adapter
import runtime_config
from cred_errors import InvalidCredentialsError


class _FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeCookieJar(dict):
    def set(self, k, v, **kw):
        self[k] = v


class _FakeSession:
    """Stand-in for requests.Session used by OnlinePlusAdapter._login.

    ``.get`` answers the login-page scrape + the captcha; ``.post`` answers the
    login. ``captcha`` is whatever the (faked) decoder should solve to.
    """

    def __init__(self, login_payload):
        self.headers = {}
        self.cookies = _FakeCookieJar()
        self.trust_env = True
        self._login_payload = login_payload

    def get(self, url, **kw):
        if "GetCaptchaImage" in url:
            return _FakeResp(200, {"Data": {"Captcha": "BASE64PNG", "CaptchaKey": "k-1"}})
        # web login-page scrape for ApiBaseURl
        return _FakeResp(200, text="var ApiBaseURl = 'https://api.hafezbroker.ir';")

    def post(self, url, **kw):
        # A real login sets the auth cookie on the session.
        self.cookies.set("AuthCookie_OnlineCookie", "abc")
        return _FakeResp(200, self._login_payload)


def _ok_login(**overrides):
    data = {
        "Token": "JWT",
        "CustomerName": "Test User",
        "BourseCode": "TEST123",
        "ActiveSms": False,
        "ActiveOtp": False,
        "MustChangePassword": False,
    }
    data.update(overrides)
    return {"IsSuccessfull": True, "Data": data}


def _install(monkeypatch, *, login_payload=None, purchasing_power=6_000_000,
             holdings_rows=None, band=(9930, 9370), max_qty=0):
    """Patch the adapter so login + reads + the RLC band never hit the network."""
    monkeypatch.setattr(onlineplus_adapter, "_SESSION_CACHE", {}, raising=True)
    monkeypatch.setattr(onlineplus_adapter, "_API_BASE_CACHE", {}, raising=True)
    # decoder solves to a valid 4-digit captcha; accept the ocr_path kwarg.
    monkeypatch.setattr(
        onlineplus_adapter, "_default_decode_captcha", lambda b64, **kw: "1234"
    )
    sess = _FakeSession(login_payload or _ok_login())
    monkeypatch.setattr(onlineplus_adapter.requests, "Session", lambda: sess)

    def fake_get(url, **kw):
        if "Accounting/Remain" in url:
            return _FakeResp(200, {"Data": {"PurchasingPower": purchasing_power}})
        if "RealtimePortfolio" in url:
            return _FakeResp(200, {"Data": holdings_rows if holdings_rows is not None else []})
        raise AssertionError(f"unexpected GET {url}")

    # _get() uses the proxy-bypassed module read session, not requests.get.
    monkeypatch.setattr(onlineplus_adapter._READ_SESSION, "get", fake_get)
    monkeypatch.setattr(
        onlineplus_adapter.rlc_price, "get_price_band", lambda isin, timeout=15: band
    )
    monkeypatch.setattr(
        onlineplus_adapter.rlc_price, "get_max_order_qty", lambda isin, timeout=15: max_qty
    )
    return sess


def _adapter(code="hafez", user="1111111111", pw="pw"):
    return onlineplus_adapter.OnlinePlusAdapter(code, user, pw)


# --------------------------------------------------------------------------
# order body
# --------------------------------------------------------------------------
def test_order_body_buy_encoding():
    body = json.loads(onlineplus_adapter._order_body("IRO1SROD0001", 1, 9930, 601))
    assert body["isin"] == "IRO1SROD0001"
    assert body["orderSide"] == 65          # Buy
    assert body["orderValidity"] == 74      # Day
    assert body["orderCount"] == "601"      # string-typed
    assert body["orderPrice"] == "9930"


def test_order_body_sell_encoding():
    body = json.loads(onlineplus_adapter._order_body("IRO1SROD0001", 2, 9370, 100))
    assert body["orderSide"] == 86          # Sell


# --------------------------------------------------------------------------
# per-broker base_domain (OnlinePlus tenants don't share a host convention)
# --------------------------------------------------------------------------
def test_base_domain_from_config_section():
    a = onlineplus_adapter.OnlinePlusAdapter(
        "dnovin", "u", "p",
        config_section={"onlineplus_base_domain": "dnovinbr.ir"},
    )
    assert a._web_base == "https://online.dnovinbr.ir"
    assert a._api_convention == "https://api.dnovinbr.ir"


def test_base_domain_absent_uses_code_convention():
    a = onlineplus_adapter.OnlinePlusAdapter("hafez", "u", "p")
    assert a._web_base == "https://online.hafezbroker.ir"
    assert a._api_convention == "https://api.hafezbroker.ir"


# --------------------------------------------------------------------------
# BUY
# --------------------------------------------------------------------------
def test_buy_cookie_only_no_bearer_no_signer(monkeypatch):
    _install(monkeypatch, purchasing_power=6_000_000, band=(9930, 9370))
    po = _adapter().prepare_order(isin="IRO1SROD0001", side=1, config_section={})
    assert po.price == 9930.0
    # fee-adjusted: floor(6_000_000 / (9930 * 1.005)) = 601
    assert po.volume == 601
    # cookie-only: NO Bearer, NO signer, cookies present.
    assert po.bearer_token is None
    assert po.signer is None
    assert po.cookies and "AuthCookie_OnlineCookie" in po.cookies
    body = json.loads(po.body)
    assert body["orderSide"] == 65 and body["orderCount"] == "601"
    assert po.order_url == "https://api.hafezbroker.ir/Web/V1/Order/Post"


def test_buy_respects_max_volume(monkeypatch):
    _install(monkeypatch, purchasing_power=6_000_000, band=(9930, 9370))
    po = _adapter().prepare_order(
        isin="IRO1SROD0001", side=1, config_section={"max_volume": "50"}
    )
    assert po.volume == 50


def test_buy_caps_at_max_order_qty(monkeypatch):
    _install(monkeypatch, purchasing_power=6_000_000, band=(9930, 9370), max_qty=100)
    po = _adapter().prepare_order(isin="IRO1SROD0001", side=1, config_section={})
    assert po.volume == 100  # mxqo cap below the fee-adjusted 601


def test_buy_uses_config_price_override(monkeypatch):
    _install(monkeypatch, purchasing_power=1_000_000)
    po = _adapter().prepare_order(
        isin="IRO1SROD0001", side=1, config_section={"price": "200"}
    )
    assert po.price == 200.0
    assert po.volume == int(1_000_000 / (200 * 1.005))


def test_buy_runtime_fallback_fee_override(monkeypatch):
    _install(monkeypatch, purchasing_power=1_000_000, band=(1000, 900))
    monkeypatch.setattr(runtime_config, "_snapshot", lambda: {"onlineplus_fallback_buy_fee": "0.01"})
    po = _adapter().prepare_order(isin="IRO1SROD0001", side=1, config_section={})
    assert po.volume == int(1_000_000 / (1000 * 1.01))


# --------------------------------------------------------------------------
# SELL
# --------------------------------------------------------------------------
def test_sell_uses_holdings(monkeypatch):
    rows = [{"SymbolISIN": "IRO1SROD0001", "RemainQuantity": 1500}]
    _install(monkeypatch, holdings_rows=rows, band=(9930, 9370))
    po = _adapter().prepare_order(isin="IRO1SROD0001", side=2, config_section={})
    assert po.price == 9370.0  # SELL at the floor
    assert po.volume == 1500
    assert json.loads(po.body)["orderSide"] == 86


def test_sell_no_holdings_raises(monkeypatch):
    _install(monkeypatch, holdings_rows=[], band=(9930, 9370))
    with pytest.raises(ValueError):
        _adapter().prepare_order(isin="IRO1SROD0001", side=2, config_section={})


def test_invalid_side_fails_closed(monkeypatch):
    """A malformed side must raise, NOT silently fall through to a SELL."""
    _install(monkeypatch, purchasing_power=6_000_000, band=(9930, 9370))
    with pytest.raises(ValueError):
        _adapter().prepare_order(isin="IRO1SROD0001", side=3, config_section={})


def test_holdings_isin_casing_fallback(monkeypatch):
    # GetOrderList casing (SymbolIsin) must also resolve in holdings.
    rows = [{"SymbolIsin": "IRO1SROD0001", "RemainQuantity": 7}]
    _install(monkeypatch, holdings_rows=rows, band=(9930, 9370))
    po = _adapter().prepare_order(isin="IRO1SROD0001", side=2, config_section={})
    assert po.volume == 7


# --------------------------------------------------------------------------
# credential / OTP handling
# --------------------------------------------------------------------------
def test_invalid_credentials_raises(monkeypatch):
    _install(
        monkeypatch,
        login_payload={"IsSuccessfull": False, "MessageCode": "oms_1000",
                       "MessageDesc": "نام کاربری /رمز عبور نامعتبر می باشد."},
    )
    with pytest.raises(InvalidCredentialsError):
        _adapter().prepare_order(isin="IRO1SROD0001", side=1, config_section={})


def test_otp_account_skipped(monkeypatch):
    _install(monkeypatch, login_payload=_ok_login(ActiveOtp=True))
    # OTP account: creds valid but not auto-tradable → RuntimeError (skipped),
    # NOT InvalidCredentialsError (which would mark the account bad-creds).
    with pytest.raises(RuntimeError) as ei:
        _adapter().prepare_order(isin="IRO1SROD0001", side=1, config_section={})
    assert "OTP" in str(ei.value) or "SMS" in str(ei.value)
    assert not isinstance(ei.value, InvalidCredentialsError)


def test_bad_captcha_retries_then_fails(monkeypatch):
    _install(monkeypatch, login_payload={"IsSuccessfull": False, "MessageCode": "InvalidCaptcha"})
    # InvalidCaptcha is retried (never a credential reject); after the budget is
    # exhausted prepare_order surfaces a RuntimeError, NOT InvalidCredentialsError.
    with pytest.raises(RuntimeError) as ei:
        _adapter().prepare_order(isin="IRO1SROD0001", side=1, config_section={})
    assert not isinstance(ei.value, InvalidCredentialsError)


# --------------------------------------------------------------------------
# host discovery
# --------------------------------------------------------------------------
def test_api_base_runtime_override(monkeypatch):
    _install(monkeypatch, purchasing_power=1_000_000)
    monkeypatch.setattr(
        runtime_config, "_snapshot",
        lambda: {"onlineplus_api_hafez": "https://api-override.example/"},
    )
    po = _adapter().prepare_order(isin="IRO1SROD0001", side=1, config_section={"price": "200"})
    assert po.order_url.startswith("https://api-override.example/Web/V1/Order/Post")


# --------------------------------------------------------------------------
# auto-sell SellContext
# --------------------------------------------------------------------------
def test_open_sell_context(monkeypatch):
    rows = [{"SymbolISIN": "IRO1SROD0001", "RemainQuantity": 1001}]
    _install(monkeypatch, holdings_rows=rows, band=(9930, 9370), max_qty=100)
    ctx = _adapter().open_sell_context(isin="IRO1SROD0001", config_section={})
    assert ctx.floor_price == 9370
    assert ctx.max_order_volume == 100
    assert ctx.fetch_holdings() == 1001
    chunk = ctx.prepare_chunk(100)
    assert chunk.price == 9370 and chunk.volume == 100
    assert chunk.signer is None and chunk.bearer_token is None
    assert chunk.cookies and "AuthCookie_OnlineCookie" in chunk.cookies
    body = json.loads(chunk.body)
    assert body["orderSide"] == 86 and body["orderCount"] == "100" and body["orderPrice"] == "9370"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
