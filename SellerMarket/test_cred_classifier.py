"""Hermetic tests for the bot's invalid-credentials detection.

NO real network. The classifiers are pure; the EphoenixAPIClient login path is
exercised with monkeypatched requests + a fixed captcha decoder. The money
invariant: a wrong CAPTCHA (errorCode -1000) keeps retrying (returns None),
while a wrong PASSWORD (errorCode 3000) raises InvalidCredentialsError so the
caller skips the account — and an unknown/ambiguous body NEVER raises.

Run:  python -m pytest test_cred_classifier.py -q
"""
from __future__ import annotations

import pytest

import api_client
from cred_errors import (
    InvalidCredentialsError,
    ephoenix_login_is_invalid_credentials,
    exir_login_is_invalid_credentials,
)


# --------------------------------------------------------------------------
# pure ephoenix classifier
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body,expected",
    [
        ({"errorCode": 3000, "token": None}, True),     # wrong password
        ({"errorCode": -1000}, False),                  # wrong captcha
        ({"errorCode": 0, "token": "jwt"}, False),      # success
        ({"token": "jwt"}, False),                      # token present
        ({"errorCode": 9999}, False),                   # unknown code
        ({}, False),
        ({"errorCode": "3000"}, False),                 # string, not int
        (None, False),
        ("<html>", False),
    ],
)
def test_ephoenix_classifier(body, expected):
    assert ephoenix_login_is_invalid_credentials(body) is expected


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"errorCode": 40037, "type": "error"}, True),    # wrong password
        ({"errorCode": 9002, "type": "error"}, False),    # wrong captcha
        ({"nt": "seed"}, False),                          # success
        ({"errorCode": 99999}, False),                    # unknown
        ({}, False),
        (None, False),
        ("نام کاربری یا کلمه عبور اشتباه است", False),     # a bare string is not a body
    ],
)
def test_exir_classifier(body, expected):
    assert exir_login_is_invalid_credentials(body) is expected


# --------------------------------------------------------------------------
# EphoenixAPIClient login path
# --------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _make_client():
    return api_client.EphoenixAPIClient(
        broker_code="bbi",
        username="u",
        password="pw",
        captcha_decoder=lambda _b: "12345",
        endpoints={"captcha": "https://c", "login": "https://l"},
        cache=None,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(api_client.time, "sleep", lambda *_a, **_k: None)


def _patch_login(monkeypatch, login_payload):
    captcha = _Resp({"captchaByteData": "x", "salt": "s", "hashedCaptcha": "h"})
    monkeypatch.setattr(api_client.requests, "get", lambda *a, **k: captcha)
    monkeypatch.setattr(api_client.requests, "post", lambda *a, **k: _Resp(login_payload))


def test_login_raises_on_wrong_password(monkeypatch):
    _patch_login(monkeypatch, {"errorCode": 3000, "token": None})
    with pytest.raises(InvalidCredentialsError):
        _make_client()._login_with_captcha()


def test_login_returns_none_on_wrong_captcha(monkeypatch):
    """errorCode -1000 → None (caller retries a fresh captcha), NOT a raise."""
    _patch_login(monkeypatch, {"errorCode": -1000})
    assert _make_client()._login_with_captcha() is None


def test_login_returns_token_on_success(monkeypatch):
    _patch_login(monkeypatch, {"errorCode": 0, "token": "jwt"})
    assert _make_client()._login_with_captcha() == "jwt"


def test_authenticate_propagates_invalid_creds_without_retry_storm(monkeypatch):
    """authenticate() must surface InvalidCredentialsError immediately, not
    grind through its 100-attempt loop."""
    client = _make_client()
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise InvalidCredentialsError("nope")

    monkeypatch.setattr(client, "_login_with_captcha", _boom)
    monkeypatch.setattr(client, "_load_token", lambda: None)
    with pytest.raises(InvalidCredentialsError):
        client.authenticate()
    assert calls["n"] == 1  # one attempt, no 100x storm


# --------------------------------------------------------------------------
# exir ExirAdapter.prepare_order must let InvalidCredentialsError through (not
# re-wrap it into RuntimeError), else the caller's skip branch never fires.
# --------------------------------------------------------------------------
def _raise(exc):
    def _f(*_a, **_k):
        raise exc
    return _f


def test_exir_prepare_order_propagates_invalid_credentials(monkeypatch):
    import exir_adapter
    a = exir_adapter.ExirAdapter("khobregan", "user", "pw")
    monkeypatch.setattr(a, "_session", _raise(InvalidCredentialsError("rejected")))
    with pytest.raises(InvalidCredentialsError):
        a.prepare_order(isin="IRO3SMBZ0001", side=1, config_section={"price": "200"})


def test_exir_prepare_order_still_wraps_generic_errors(monkeypatch):
    """A genuine network/parse failure is still wrapped in RuntimeError."""
    import exir_adapter
    a = exir_adapter.ExirAdapter("khobregan", "user", "pw")
    monkeypatch.setattr(a, "_session", _raise(ConnectionError("network down")))
    with pytest.raises(RuntimeError):
        a.prepare_order(isin="IRO3SMBZ0001", side=1, config_section={"price": "200"})
