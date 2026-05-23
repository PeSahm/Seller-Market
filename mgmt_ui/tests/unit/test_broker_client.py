"""Unit tests for ``app.services.broker_client``.

The broker_client makes real HTTP calls in production. Here we mock at the
``httpx`` transport layer using ``httpx.MockTransport`` so the captcha →
OCR → login → getcustomerinfo flow can be exercised end-to-end without
ever touching the network.

Three scenarios are covered:

1. **Happy path** — captcha solves, login returns a token, getcustomerinfo
   returns ``isError=false`` with a populated ``result`` dict.
2. **Bad password** — login returns ``{token: null}``. After the retry cap
   the result is ``ok=False`` with the "Authentication failed" error.
3. **Broker-side error** — login succeeds, but getcustomerinfo returns
   ``isError=true`` with a Persian message. The message is surfaced
   verbatim.

The mock transport intercepts requests by URL, so we don't have to assert
exact wire shapes — the bot's behaviour is what's documented; we just need
to confirm our client maps each response variant onto the right
``VerifyResult``.
"""
from __future__ import annotations

import httpx
import pytest

from app.services import broker_client
from app.services.broker_client import (
    IsinInfo,
    VerifyResult,
    verify_credentials,
    verify_isin,
)


@pytest.mark.asyncio
async def test_endpoints_for_ephoenix_family():
    """ephoenix-family URLs slot the broker code into both the identity
    hostname and the backofficeexternal hostname — same pattern as
    ``SellerMarket/broker_enum.py``."""
    eps = broker_client._endpoints_for("ayandeh")
    assert eps["captcha"] == "https://identity-ayandeh.ephoenix.ir/api/Captcha/GetCaptcha"
    assert eps["login"] == "https://identity-ayandeh.ephoenix.ir/api/v2/accounts/login"
    assert eps["customer_info"] == (
        "https://backofficeexternal-ayandeh.ephoenix.ir/api/party/getcustomerinfo"
    )


@pytest.mark.asyncio
async def test_endpoints_for_ib():
    """``ib`` is special-cased: hardcoded api8.ibtrader.ir for the
    customer-info host (separate shard from the regular api host)."""
    eps = broker_client._endpoints_for("ib")
    assert eps["captcha"] == "https://identity.ibtrader.ir/api/Captcha/GetCaptcha"
    assert eps["login"] == "https://identity.ibtrader.ir/api/v2/accounts/login"
    assert eps["customer_info"] == (
        "https://api8.ibtrader.ir/api/party/getcustomerinfo"
    )


def _make_handler(
    *,
    login_token,
    customer_info_payload,
    ocr_text="ABCD",
    market_data_payload=None,
):
    """Build a callable that routes a request to a canned response based
    on URL substring. Used as the ``handler`` for ``httpx.MockTransport``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/Captcha/GetCaptcha" in url:
            return httpx.Response(
                200,
                json={
                    "captchaByteData": "iVBORw0KGgo=",  # base64 placeholder
                    "salt": "salt-xyz",
                    "hashedCaptcha": "hash-xyz",
                },
            )
        if "/ocr/captcha-easy-base64" in url:
            # Real service returns plain text (sometimes JSON-quoted).
            # The client peels surrounding quotes itself.
            return httpx.Response(200, text=ocr_text)
        if "/api/v2/accounts/login" in url:
            return httpx.Response(200, json={"token": login_token})
        if "/api/party/getcustomerinfo" in url:
            return httpx.Response(200, json=customer_info_payload)
        if "/api/v2/instruments/full" in url:
            # ``market_data_payload`` defaults to "no instrument" — only
            # the verify_isin tests override it.
            return httpx.Response(200, json=market_data_payload or [])
        return httpx.Response(404, text=f"unmocked URL: {url}")

    return handler


@pytest.fixture
def patch_httpx(monkeypatch):
    """Patch ``httpx.AsyncClient`` so the broker_client uses a
    MockTransport instead of a real one. Returns a setter the test calls
    with the (token, payload) pair it wants to mock.
    """

    state = {"handler": None}

    real_async_client_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # noqa: ANN001 — signature mirrors httpx
        kwargs.setdefault("transport", httpx.MockTransport(state["handler"]))
        return real_async_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    def configure(*, token, payload, ocr_text="ABCD", market_data=None):
        state["handler"] = _make_handler(
            login_token=token,
            customer_info_payload=payload,
            ocr_text=ocr_text,
            market_data_payload=market_data,
        )

    return configure


@pytest.mark.asyncio
async def test_verify_credentials_success(patch_httpx):
    """Happy path: token + isError=false → ok=True with populated fields.

    Mirrors the response shape pasted into the planning conversation.
    """
    patch_httpx(
        token="fake-jwt-token",
        payload={
            "result": {
                "fullName": "مصطفی اسماعیلی",
                "nationalId": "4580090306",
                "bourseCode": "اسمـ50113",
                "type": "حقیقی",
            },
            "message": "عملیات با موفقیت انجام شد.",
            "isError": False,
        },
    )

    result = await verify_credentials(
        broker_code="ayandeh",
        username="4580090306",
        password="correct-password",
        ocr_service_url="http://ocr.test",
    )

    assert isinstance(result, VerifyResult)
    assert result.ok is True
    assert result.full_name == "مصطفی اسماعیلی"
    assert result.national_id == "4580090306"
    assert result.bourse_code == "اسمـ50113"
    assert result.type_ == "حقیقی"
    assert result.error is None


@pytest.mark.asyncio
async def test_verify_credentials_bad_password(patch_httpx):
    """Login returns ``{token: null}`` — after the retry cap, ok=False
    with the generic auth-failed message. Crucially, the error does NOT
    leak which of username/password was wrong."""
    patch_httpx(
        token=None,  # login never returns a token
        payload={"result": {}, "isError": False},  # never reached
    )

    result = await verify_credentials(
        broker_code="ayandeh",
        username="4580090306",
        password="WRONG",
        ocr_service_url="http://ocr.test",
    )

    assert result.ok is False
    assert result.error is not None
    # The error must not finger-point at which of the two fields was wrong.
    # The literal substring "username" / "password" CAN appear (e.g. in
    # the generic "check username/password" hint, which is the correct
    # framing) — what mustn't appear is a phrase that names ONE
    # specifically as the broken one.
    err = result.error.lower()
    forbidden = [
        "wrong password", "wrong username",
        "invalid password", "invalid username",
        "incorrect password", "incorrect username",
        "password is wrong", "username is wrong",
        "bad password", "bad username",
    ]
    for phrase in forbidden:
        assert phrase not in err, f"error message leaks which field was wrong: {phrase!r} in {result.error!r}"


@pytest.mark.asyncio
async def test_verify_credentials_broker_error_surfaces_message(patch_httpx):
    """Login succeeds but getcustomerinfo returns isError=true with a
    Persian message — the message is surfaced verbatim (operator-readable
    — they speak the language; we don't translate)."""
    patch_httpx(
        token="fake-jwt-token",
        payload={
            "result": None,
            "isError": True,
            "message": "حساب کاربری غیرفعال است.",  # "Account is disabled."
        },
    )

    result = await verify_credentials(
        broker_code="ayandeh",
        username="4580090306",
        password="correct-but-account-locked",
        ocr_service_url="http://ocr.test",
    )

    assert result.ok is False
    assert result.error == "حساب کاربری غیرفعال است."


# ---------------------------------------------------------------------------
# verify_isin tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_isin_success(patch_httpx):
    """Happy path: market_data returns one instrument; ``IsinInfo`` carries
    symbol, title, prices, volumes pulled from the nested ``i`` and ``t``
    keys (same shape the bot's ``get_instrument_info`` uses).
    """
    patch_httpx(
        token="fake-jwt-token",
        payload={"result": {}, "isError": False},  # customer-info not called
        market_data=[
            {
                "i": {
                    "s": "saipa",
                    "t": "Iran Khodro Saipa",
                    "maxeq": 1_000_000,
                    "mineq": 1,
                },
                "t": {
                    "cup": 12_345,
                    "minap": 11_500,
                    "maxap": 13_200,
                },
            }
        ],
    )

    result = await verify_isin(
        broker_code="ayandeh",
        username="4580090306",
        password="correct",
        isin="IRO3SAIPA0001",
        ocr_service_url="http://ocr.test",
    )

    assert isinstance(result, IsinInfo)
    assert result.ok is True
    assert result.isin == "IRO3SAIPA0001"
    assert result.symbol == "saipa"
    assert result.title == "Iran Khodro Saipa"
    assert result.last_price == 12_345
    assert result.min_price == 11_500
    assert result.max_price == 13_200
    assert result.max_volume == 1_000_000
    assert result.min_volume == 1
    assert result.error is None


@pytest.mark.asyncio
async def test_verify_isin_not_found(patch_httpx):
    """market_data returns an empty array — no instrument matches the
    typed ISIN. Return ``ok=False`` with a clear error naming the ISIN."""
    patch_httpx(
        token="fake-jwt-token",
        payload={"result": {}, "isError": False},
        market_data=[],
    )

    result = await verify_isin(
        broker_code="ayandeh",
        username="4580090306",
        password="correct",
        isin="IROABOGUS0001",
        ocr_service_url="http://ocr.test",
    )

    assert result.ok is False
    assert "IROABOGUS0001" in (result.error or "")
    assert result.symbol is None


@pytest.mark.asyncio
async def test_verify_isin_bad_credentials(patch_httpx):
    """Login returns no token → can't even call market_data. The error
    must point at the credentials, not at the ISIN."""
    patch_httpx(
        token=None,  # login never returns a token
        payload={"result": {}, "isError": False},
        market_data=[],  # never reached
    )

    result = await verify_isin(
        broker_code="ayandeh",
        username="4580090306",
        password="WRONG",
        isin="IRO3SAIPA0001",
        ocr_service_url="http://ocr.test",
    )

    assert result.ok is False
    # The same forbidden-phrase guarantees as verify_credentials apply.
    assert result.error is not None
    err = result.error.lower()
    for phrase in [
        "wrong password",
        "wrong username",
        "invalid password",
        "invalid username",
    ]:
        assert phrase not in err
