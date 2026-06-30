"""Hermetic tests for the Mofid / Orbis bot adapter (no network).

The full OAuth2/PKCE redirect chain, the BotDetect captcha, the Bearer reads
(buying power / holdings / server-time), the draft+batch firing prep, and the RLC
band are all faked. Asserts: the Bearer order shape (no signer/cookies) with the
Referer+UA extra_headers, the draft body side encoding (Buy=0/Sell=1), the
fee-adjusted BUY sizing, N-draft creation, the batch body, ``validate`` creating
NO drafts, the wrong-password vs wrong-captcha handling, and the auto-sell
SellContext. Wire shapes confirmed by the Phase-0 spike (scratch/MOFID_FINDINGS.md).

Run: ``python -m pytest test_mofid_adapter.py -q``.
"""
from __future__ import annotations

import json

import pytest
import requests

import mofid_adapter
import runtime_config
from cred_errors import InvalidCredentialsError


class _Resp:
    def __init__(self, status=200, headers=None, text="", payload=None, content=b""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text
        self._payload = payload
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


_LOGIN_HTML = '<input name="__RequestVerificationToken" value="TOK"/>'
_CAPTCHA_HTML = (
    _LOGIN_HTML
    + '<input name="Captcha"/>'
    + '<img id="OLoginCaptcha_CaptchaImage" src="/captcha.jpg"/>'
    + '<input name="BDC_VCID_OLoginCaptcha" value="v"/>'
    + '<input name="BDC_BackWorkaround_OLoginCaptcha" value="b"/>'
    + '<input name="BDC_Hs_OLoginCaptcha" value="h"/>'
    + '<input name="BDC_SP_OLoginCaptcha" value="s"/>'
)


class _FakeLoginSession:
    """Drives the 8-step OAuth chain. ``reject`` forces a login-POST failure;
    ``captcha`` makes the login page carry the BotDetect captcha."""

    def __init__(self, *, reject=None, captcha=False):
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.trust_env = True
        self.reject = reject
        self.captcha = captcha
        self.login_posts = 0
        self.captcha_posted = None

    def get(self, url, params=None, **kw):
        if "/connect/authorize/callback" in url and params is not None:
            # Step 1 — authorize → /Login
            return _Resp(302, headers={"Location": "/Login?ReturnUrl=%2Fconnect%2Fauthorize"})
        if "/Login" in url:
            return _Resp(200, text=_CAPTCHA_HTML if self.captcha else _LOGIN_HTML)
        if "captcha" in url.lower():
            return _Resp(200, content=b"PNGBYTES")
        if "/connect/authorize" in url:
            # Step 6 — authorize-continue → auth-callback?code=
            return _Resp(302, headers={"Location": "https://d.easytrader.ir/auth-callback?code=ABC&x=1"})
        return _Resp(200, text="")

    def post(self, url, data=None, json=None, **kw):
        if url.endswith("/connect/token"):
            return _Resp(200, payload={"access_token": "JWT", "expires_in": 43200})
        if "same-login" in url:
            return _Resp(200)
        # the login POST
        self.login_posts += 1
        if data and "Captcha" in data:
            self.captcha_posted = data["Captcha"]
        if self.reject == "invalid_credentials":
            return _Resp(200, text='<div class="validation-summary-errors">نام کاربری یا کلمه عبور نادرست است</div>')
        if self.reject == "wrong_captcha":
            return _Resp(200, text='<div class="validation-summary-errors">کد امنیتی اشتباه است</div>')
        return _Resp(302, headers={"Location": "/connect/authorize/callback?continue=1"})


class _FakeReadSession:
    """Stands in for the module ``_READ_SESSION`` (Bearer reads + draft POSTs)."""

    def __init__(self, *, buy_power=31_224_500, holdings=None, draft_id=7):
        self.buy_power = buy_power
        self.holdings = holdings if holdings is not None else []
        self.draft_id = draft_id
        self.draft_posts = 0

    def get(self, url, **kw):
        if "/core/api/money/" in url:
            return _Resp(200, payload={"buyPower": self.buy_power})
        if "/core/api/portfolio/true" in url:
            return _Resp(200, payload={"portfolioItems": self.holdings})
        if "/server-time/" in url:
            return _Resp(200, payload={"diff": 1500})
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, **kw):
        if url.endswith("/easy/api/draft"):
            self.draft_posts += 1
            return _Resp(200, payload={"id": self.draft_id})
        raise AssertionError(f"unexpected POST {url}")


def _install(monkeypatch, *, reject=None, captcha=False, buy_power=31_224_500,
             holdings=None, band=(20930, 19730), max_qty=0):
    monkeypatch.setattr(mofid_adapter, "_SESSION_CACHE", {}, raising=True)
    monkeypatch.setattr(mofid_adapter, "_default_decode_captcha", lambda b64, **kw: "ABCDE")
    # keep tests file-free: never read/write the persistent token cache
    monkeypatch.setattr(mofid_adapter.MofidAdapter, "_read_token_file", lambda self: None)
    monkeypatch.setattr(mofid_adapter.MofidAdapter, "_write_token_file", lambda self, t, e: None)
    sess = _FakeLoginSession(reject=reject, captcha=captcha)
    monkeypatch.setattr(mofid_adapter.requests, "Session", lambda: sess)
    read = _FakeReadSession(buy_power=buy_power, holdings=holdings)
    monkeypatch.setattr(mofid_adapter._READ_SESSION, "get", read.get)
    monkeypatch.setattr(mofid_adapter._READ_SESSION, "post", read.post)
    monkeypatch.setattr(mofid_adapter.rlc_price, "get_price_band", lambda isin, timeout=15: band)
    monkeypatch.setattr(mofid_adapter.rlc_price, "get_max_order_qty", lambda isin, timeout=15: max_qty)
    return sess, read


def _adapter(code="mofid", user="1111111111", pw="pw", **kw):
    return mofid_adapter.MofidAdapter(code, user, pw, **kw)


# -------------------------------------------------------------------------- pure
def test_side_encoding():
    assert mofid_adapter._mofid_side(1) == 0  # buy
    assert mofid_adapter._mofid_side(2) == 1  # sell


def test_response_ok():
    ok = mofid_adapter.mofid_response_ok
    assert ok(200, b'{"isSuccessful":true}') is True
    assert ok(200, b"null") is True  # empty 200
    assert ok(200, b'{"isSuccessful":false}') is False
    assert ok(200, b'{"omsError":[{"code":8706}]}') is False  # market closed
    assert ok(200, b'{"error":"boom"}') is False
    assert ok(500, b"") is False
    assert ok(200, b"<html>not json") is False


# -------------------------------------------------------------------------- BUY
def test_buy_draft_batch_bearer(monkeypatch):
    _sess, read = _install(monkeypatch, buy_power=31_224_500, band=(20930, 19730))
    po = _adapter().prepare_order(isin="IRO1MSMI0001", side=1, config_section={})
    assert po.price == 20930.0
    # fee-adjusted: floor(31_224_500 / (20930 * 1.005)) = 1484
    assert po.volume == int(31_224_500 / (20930 * 1.005)) == 1484
    # Bearer family: token set, no signer/cookies; Mofid headers present.
    assert po.bearer_token == "JWT"
    assert po.signer is None and po.cookies is None
    assert po.extra_headers["Referer"] == mofid_adapter.REFERER
    assert "Chrome/131" in po.extra_headers["User-Agent"]
    # default draft_count=1 → one draft POST, batch references its id
    assert read.draft_posts == 1
    assert po.order_url.endswith("/core/api/order/batchCreate")
    batch = json.loads(po.body)
    assert batch == {"draftIds": [7], "removeDraftAfterCreate": False, "orderFrom": 34}


def test_draft_count_runtime_override(monkeypatch):
    _sess, read = _install(monkeypatch, buy_power=31_224_500)
    monkeypatch.setattr(runtime_config, "_snapshot", lambda: {"mofid_draft_count": "3"})
    po = _adapter().prepare_order(isin="IRO1MSMI0001", side=1, config_section={})
    assert read.draft_posts == 3
    assert json.loads(po.body)["draftIds"] == [7, 7, 7]


def test_buy_caps_at_max_order_qty(monkeypatch):
    _install(monkeypatch, buy_power=31_224_500, band=(20930, 19730), max_qty=100)
    po = _adapter().prepare_order(isin="IRO1MSMI0001", side=1, config_section={})
    assert po.volume == 100


def test_buy_respects_max_volume(monkeypatch):
    _install(monkeypatch, buy_power=31_224_500)
    po = _adapter().prepare_order(isin="IRO1MSMI0001", side=1, config_section={"max_volume": "50"})
    assert po.volume == 50


def test_buy_price_override(monkeypatch):
    _install(monkeypatch, buy_power=1_000_000)
    po = _adapter().prepare_order(isin="X", side=1, config_section={"price": "200"})
    assert po.price == 200.0
    assert po.volume == int(1_000_000 / (200 * 1.005))


def test_captcha_solved_and_posted(monkeypatch):
    sess, _read = _install(monkeypatch, captcha=True, buy_power=1_000_000)
    po = _adapter().prepare_order(isin="X", side=1, config_section={"price": "100"})
    assert po.bearer_token == "JWT"
    assert sess.captcha_posted == "ABCDE"  # decoder output went into the form


# ------------------------------------------------------------------- validate
def test_validate_creates_no_drafts(monkeypatch):
    _sess, read = _install(monkeypatch, buy_power=31_224_500, band=(20930, 19730))
    po = _adapter().validate(isin="IRO1MSMI0001", side=1, config_section={})
    assert read.draft_posts == 0  # NO side effect in warmup
    assert po.volume == 1484 and po.price == 20930.0
    assert po.order_url.endswith("/core/api/v2/order")
    body = json.loads(po.body)
    assert body["order"]["side"] == 0  # buy


# ----------------------------------------------------------------------- SELL
def test_sell_via_open_sell_context(monkeypatch):
    rows = [{"isin": "IRO1MSMI0001", "asset": 1001}]
    _install(monkeypatch, holdings=rows, band=(20930, 19730), max_qty=100)
    ctx = _adapter().open_sell_context(isin="IRO1MSMI0001", config_section={})
    assert ctx.floor_price == 19730
    assert ctx.max_order_volume == 100
    assert ctx.fetch_holdings() == 1001
    chunk = ctx.prepare_chunk(100)
    assert chunk.bearer_token == "JWT" and chunk.signer is None and chunk.cookies is None
    body = json.loads(chunk.body)
    assert body["order"]["side"] == 1  # sell
    assert body["order"]["price"] == "19730" and body["order"]["quantity"] == "100"


# --------------------------------------------------------------- credentials
def test_invalid_credentials_raises(monkeypatch):
    _install(monkeypatch, reject="invalid_credentials")
    with pytest.raises(InvalidCredentialsError):
        _adapter().prepare_order(isin="X", side=1, config_section={"price": "100"})


def test_wrong_captcha_retries_then_runtimeerror(monkeypatch):
    _install(monkeypatch, reject="wrong_captcha")
    with pytest.raises(RuntimeError) as ei:
        _adapter().prepare_order(isin="X", side=1, config_section={"price": "100"})
    assert not isinstance(ei.value, InvalidCredentialsError)


def test_invalid_side_fails_closed(monkeypatch):
    _install(monkeypatch, buy_power=1_000_000)
    with pytest.raises(ValueError):
        _adapter().prepare_order(isin="X", side=3, config_section={})


def test_server_time_offset(monkeypatch):
    _install(monkeypatch)
    assert _adapter().server_time_offset_ms() == 1500


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
