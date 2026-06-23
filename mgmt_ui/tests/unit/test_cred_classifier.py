"""Unit tests for the credential-status classifiers (ephoenix + exir).

The classifiers decide VALID / INVALID_CREDENTIALS / TRANSIENT from a broker
login response. The CRITICAL invariant is conservatism: an UNKNOWN / ambiguous
response must NEVER classify as INVALID_CREDENTIALS (a false invalid would stop
a good account trading). Markers are from the live probe — see
SellerMarket/scratch/CRED_STATUS_FINDINGS.md.
"""
from __future__ import annotations

import httpx
import pytest

from app.services import broker_client
from app.services.broker_client import _classify_ephoenix_login, verify_credentials
from app.services.brokers import exir as exir_mod
from app.services.brokers.base import CredStatus, VerifyResult


# --------------------------------------------------------------------------
# ephoenix / ibtrader — pure classifier (keys on numeric errorCode)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,body,expected",
    [
        # success: token present + errorCode 0
        (200, {"errorCode": 0, "token": "jwt", "isSuccess": True}, CredStatus.VALID),
        # token present even without errorCode 0 → VALID (token wins)
        (200, {"token": "jwt"}, CredStatus.VALID),
        # wrong password → high-confidence reject
        (200, {"errorCode": 3000, "token": None, "isSuccess": False},
         CredStatus.INVALID_CREDENTIALS),
        # wrong captcha → ambiguous (retry), NOT invalid
        (200, {"errorCode": -1000, "isSuccess": False}, None),
        # unknown error code → ambiguous
        (200, {"errorCode": 9999}, None),
        # missing errorCode, no token → ambiguous
        (200, {"isSuccess": False}, None),
        # non-dict bodies → ambiguous
        (200, None, None),
        (200, "<html>error</html>", None),
        (200, [], None),
    ],
)
def test_classify_ephoenix_login(status, body, expected):
    assert _classify_ephoenix_login(status, body) is expected


def test_classify_ephoenix_never_invalid_on_unknown():
    """The money invariant: only errorCode 3000 yields INVALID_CREDENTIALS;
    every other shape stays ambiguous (→ TRANSIENT upstream)."""
    for body in (
        {},
        {"errorCode": -1000},
        {"errorCode": 1018, "errorMessage": "rate limited"},
        {"message": "some persian text"},
        {"errorCode": "3000"},  # string, not int — deliberately not matched
    ):
        assert _classify_ephoenix_login(200, body) is not CredStatus.INVALID_CREDENTIALS


# --------------------------------------------------------------------------
# exir — pure login classifier (keys on numeric errorCode, LIVE-confirmed)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body,expected",
    [
        ({"errorCode": 40037, "type": "error"}, True),   # wrong password (HTTP 403)
        ({"errorCode": 9002, "type": "error"}, False),   # wrong captcha (HTTP 401)
        ({"nt": "seed"}, False),                         # success
        ({"errorCode": 99999}, False),                   # unknown
        ({}, False),
        (None, False),
        ("نام کاربری یا کلمه عبور اشتباه است", False),    # a bare string is not a body
    ],
)
def test_classify_exir_login(body, expected):
    assert exir_mod._classify_exir_login(body) is expected


# --------------------------------------------------------------------------
# VerifyResult default + end-to-end wiring through verify_credentials
# --------------------------------------------------------------------------
def test_verify_result_defaults_transient():
    """A bare VerifyResult is TRANSIENT — never accidentally 'valid'/'invalid'."""
    assert VerifyResult(ok=False).status is CredStatus.TRANSIENT


def _login_only_handler(*, error_code, token):
    """MockTransport handler that returns a canned login body (errorCode/token)
    plus a happy getcustomerinfo, so verify_credentials runs end-to-end."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/Captcha/GetCaptcha" in url:
            return httpx.Response(200, json={
                "captchaByteData": "iVBORw0KGgo=", "salt": "s", "hashedCaptcha": "h"})
        if "/ocr/captcha-easy-base64" in url:
            return httpx.Response(200, text="1234")
        if "/api/v2/accounts/login" in url:
            return httpx.Response(200, json={"errorCode": error_code, "token": token})
        if "/api/party/getcustomerinfo" in url:
            return httpx.Response(200, json={"isError": False, "result": {
                "fullName": "علی", "nationalId": "x", "bourseCode": "b"}})
        return httpx.Response(404, text=f"unmocked: {url}")

    return handler


@pytest.fixture
def patch_login(monkeypatch):
    state = {"handler": None}
    real_init = httpx.AsyncClient.__init__

    def patched(self, *a, **kw):
        kw.setdefault("transport", httpx.MockTransport(state["handler"]))
        return real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    broker_client._TOKEN_CACHE.clear()
    yield state
    broker_client._TOKEN_CACHE.clear()


@pytest.mark.asyncio
async def test_verify_credentials_invalid_sets_status(patch_login):
    """errorCode 3000 → ok=False AND status=INVALID_CREDENTIALS (the actionable
    signal the dashboard/worker/bot key on)."""
    patch_login["handler"] = _login_only_handler(error_code=3000, token=None)
    res = await verify_credentials("bbi", "u", "wrong-pw", "http://ocr")
    assert res.ok is False
    assert res.status is CredStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_verify_credentials_bad_captcha_is_transient(patch_login):
    """errorCode -1000 (wrong captcha) every attempt → retries exhaust → ok=False
    but status=TRANSIENT (NOT invalid — never alarm on a captcha miss)."""
    patch_login["handler"] = _login_only_handler(error_code=-1000, token=None)
    res = await verify_credentials("bbi", "u", "pw", "http://ocr")
    assert res.ok is False
    assert res.status is CredStatus.TRANSIENT


@pytest.mark.asyncio
async def test_verify_credentials_success_sets_valid(patch_login):
    """errorCode 0 + token → ok=True, status=VALID."""
    patch_login["handler"] = _login_only_handler(error_code=0, token="jwt")
    res = await verify_credentials("bbi", "u", "pw", "http://ocr")
    assert res.ok is True
    assert res.status is CredStatus.VALID


# --------------------------------------------------------------------------
# The verify-result partial must expose data-cred-status — the contract the
# verify-on-save JS branches on. A render smoke pins it.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "result,expected_attr",
    [
        (VerifyResult(ok=True, status=CredStatus.VALID, full_name="x"), "valid"),
        (VerifyResult(ok=False, status=CredStatus.INVALID_CREDENTIALS,
                      error="bad"), "invalid_credentials"),
        (VerifyResult(ok=False, status=CredStatus.TRANSIENT, error="down"),
         "transient"),
    ],
)
def test_verify_result_partial_exposes_status(result, expected_attr):
    from app.routers.dashboard import templates
    html = templates.env.get_template(
        "partials/customer_verify_result.html"
    ).render(result=result, typed_username="u")
    assert 'data-cred-status="' + expected_attr + '"' in html
