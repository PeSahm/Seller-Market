"""Broker credential verification client.

Used by the admin add-customer form's *Verify credentials* button. Calls the
broker's ``/api/party/getcustomerinfo`` endpoint with the operator-typed
username/password and surfaces the broker-confirmed ``fullName`` so the
operator can sanity-check the credentials before saving the row.

This module deliberately does NOT import from the bot package
(``SellerMarket/api_client.py``) — the mgmt UI and the bot run in separate
containers with separate dependency sets. Instead we duplicate the
captcha-login + getcustomerinfo wire-shape using ``httpx``.

Mirrors the bot's flow at ``SellerMarket/api_client.py::_login_with_captcha``
and ``get_customer_info``:

1. GET broker's captcha endpoint → returns ``captchaByteData`` + ``salt``
   + ``hashedCaptcha``.
2. POST the ``captchaByteData`` to the OCR microservice → decoded captcha
   string.
3. POST ``loginName`` + ``password`` + ``captcha`` to the broker's login
   endpoint → Bearer token.
4. POST empty body to ``/api/party/getcustomerinfo`` with the Bearer token →
   the customer-info record.

The captcha solve can fail intermittently — we cap retries at 5 to keep the
verify latency bounded to a few seconds (the bot uses 100, but that's a
batch background job, not an interactive button).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Cap captcha-solve retries at 5 so the button returns in seconds. The bot
# uses 100 but that's a batch background job; here the user is staring at a
# spinner.
_MAX_LOGIN_RETRIES = 5

# Per-step timeouts in seconds. Generous enough for Iranian-VPS latency but
# short enough that a hung broker host doesn't pin the button forever.
_HTTP_TIMEOUT_S = 10.0


@dataclass
class VerifyResult:
    """Outcome of a credential verification.

    Exactly one of ``ok=True`` (with ``full_name`` populated) or
    ``ok=False`` (with ``error`` populated) holds. The other broker-side
    sanity fields are populated only on success.
    """

    ok: bool
    full_name: Optional[str] = None
    national_id: Optional[str] = None
    bourse_code: Optional[str] = None
    type_: Optional[str] = None
    message: Optional[str] = None  # broker's human-readable status, even on success
    error: Optional[str] = None  # operator-facing error explanation


def _endpoints_for(broker_code: str) -> dict[str, str]:
    """Return the captcha / login / customer_info URL trio for a broker.

    Duplicates the URL-construction logic in
    ``SellerMarket/broker_enum.py::BrokerCode.get_endpoints``. We do NOT
    import from the bot package (see module docstring) so the small bit of
    duplication is the price of independence.
    """
    if broker_code == "ib":
        domain = "ibtrader.ir"
        prefix = "."
        return {
            "captcha": f"https://identity{prefix}{domain}/api/Captcha/GetCaptcha",
            "login": f"https://identity{prefix}{domain}/api/v2/accounts/login",
            "customer_info": "https://api8.ibtrader.ir/api/party/getcustomerinfo",
        }
    # ephoenix family — same prefix shape as the bot.
    domain = "ephoenix.ir"
    prefix = f"-{broker_code}."
    return {
        "captcha": f"https://identity{prefix}{domain}/api/Captcha/GetCaptcha",
        "login": f"https://identity{prefix}{domain}/api/v2/accounts/login",
        "customer_info": (
            f"https://backofficeexternal{prefix}{domain}"
            "/api/party/getcustomerinfo"
        ),
    }


async def _solve_captcha(
    client: httpx.AsyncClient,
    ocr_service_url: str,
    captcha_byte_data: str,
) -> Optional[str]:
    """Send a captcha image to the OCR microservice and return the decoded text.

    Returns ``None`` if the OCR service can't decode this captcha (empty
    body after stripping).

    Mirrors the wire contract in
    ``SellerMarket/captcha_utils.py::decode_captcha``:

    * ``POST {ocr_service_url}/ocr/captcha-easy-base64``
    * headers: ``Content-Type: application/json``, ``accept: text/plain``
    * body: ``{"base64": "<base64-image-string>"}``
    * response body is the decoded text in plain text, occasionally wrapped
      in JSON-style double quotes — peel them off.
    """
    url = ocr_service_url.rstrip("/") + "/ocr/captcha-easy-base64"
    resp = await client.post(
        url,
        json={"base64": captcha_byte_data},
        headers={"accept": "text/plain", "Content-Type": "application/json"},
        timeout=_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    text = (resp.text or "").strip()
    # Some OCR backends return ``"ABCD"`` (quoted) — peel them.
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return text or None


async def _login_once(
    client: httpx.AsyncClient,
    endpoints: dict[str, str],
    username: str,
    password: str,
    ocr_service_url: str,
) -> Optional[str]:
    """Fetch one captcha, decode it, POST login. Return token on success, else None.

    Any HTTP-level failure raises — the retry wrapper distinguishes those
    from "captcha decoded but creds rejected".
    """
    captcha_resp = await client.get(endpoints["captcha"], timeout=_HTTP_TIMEOUT_S)
    captcha_resp.raise_for_status()
    # The broker is intermittently flaky and can return HTML error pages or
    # truncated bodies on overload. Catch malformed JSON / missing keys here
    # rather than letting them propagate as a 500 to the operator.
    try:
        cdata = captcha_resp.json()
        captcha_bytes = cdata["captchaByteData"]
        captcha_hash = cdata["hashedCaptcha"]
        captcha_salt = cdata["salt"]
    except (ValueError, KeyError, TypeError) as exc:
        body_excerpt = captcha_resp.text[:200] if captcha_resp.text else "<empty>"
        logger.warning("malformed captcha response: %s — body: %r", exc, body_excerpt)
        raise httpx.HTTPError(f"malformed captcha response: {exc}") from exc

    captcha_value = await _solve_captcha(client, ocr_service_url, captcha_bytes)
    if not captcha_value:
        # Treat OCR returning empty/blank as a "retry" signal (the image
        # may have been ambiguous) rather than a hard failure.
        return None

    login_resp = await client.post(
        endpoints["login"],
        json={
            "loginName": username,
            "password": password,
            "captcha": {
                "hash": captcha_hash,
                "salt": captcha_salt,
                "value": captcha_value,
            },
        },
        timeout=_HTTP_TIMEOUT_S,
    )
    # Don't raise_for_status here — the broker may return 200 with an
    # error JSON, or 4xx for bad creds. We classify in the caller based
    # on whether ``token`` is present.
    if login_resp.status_code >= 500:
        login_resp.raise_for_status()
    try:
        body = login_resp.json() if login_resp.content else {}
    except ValueError:
        # Login returned non-JSON — treat as a transient failure that the
        # retry loop will see and (probably) re-attempt.
        logger.warning(
            "login response was not JSON (status=%s, body=%r)",
            login_resp.status_code,
            login_resp.text[:200] if login_resp.text else "<empty>",
        )
        return None
    return body.get("token") or None


async def verify_credentials(
    broker_code: str,
    username: str,
    password: str,
    ocr_service_url: str,
) -> VerifyResult:
    """Verify broker credentials and return the broker-side customer info.

    See module docstring for the full flow.
    """
    endpoints = _endpoints_for(broker_code)

    async with httpx.AsyncClient() as client:
        token: Optional[str] = None
        last_error: Optional[str] = None

        for attempt in range(1, _MAX_LOGIN_RETRIES + 1):
            try:
                token = await _login_once(
                    client, endpoints, username, password, ocr_service_url
                )
            except httpx.HTTPError as exc:
                # Transport-level failure on captcha / OCR / login.
                # Retry on the next loop iteration — captchas are flaky;
                # the broker host occasionally returns 502.
                last_error = f"login attempt {attempt} failed: {exc}"
                logger.warning(last_error)
                continue
            if token:
                break

        if not token:
            return VerifyResult(
                ok=False,
                error=(
                    last_error
                    or "Authentication failed — check username/password "
                    f"(captcha solve gave up after {_MAX_LOGIN_RETRIES} attempts)"
                ),
            )

        # Token in hand — call getcustomerinfo.
        try:
            info_resp = await client.post(
                endpoints["customer_info"],
                headers={
                    "authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36"
                    ),
                },
                json={},
                timeout=_HTTP_TIMEOUT_S,
            )
            info_resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.exception("getcustomerinfo HTTP error")
            return VerifyResult(
                ok=False,
                error=f"cannot reach broker customer-info endpoint: {exc}",
            )

        payload = info_resp.json() or {}
        if payload.get("isError"):
            # Persian message — operator-readable. Surface verbatim.
            return VerifyResult(
                ok=False,
                error=payload.get("message") or "broker returned isError=true",
            )

        result = payload.get("result") or {}
        return VerifyResult(
            ok=True,
            full_name=result.get("fullName") or None,
            national_id=result.get("nationalId") or None,
            bourse_code=result.get("bourseCode") or None,
            type_=result.get("type") or None,
            message=payload.get("message") or None,
        )
