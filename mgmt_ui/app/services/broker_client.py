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

import hashlib
import logging
import time
from typing import Optional

import httpx

# VerifyResult / IsinInfo historically lived in this module. They now live in
# app.services.brokers.base (shared across families); re-export them here so
# existing callers/tests that do ``from app.services.broker_client import
# VerifyResult`` keep working unchanged.
from app.services.brokers.base import IsinInfo, VerifyResult  # noqa: F401  (re-export)
from app.services.brokers import registry

logger = logging.getLogger(__name__)


# Cap captcha-solve retries at 5 so the button returns in seconds. The bot
# uses 100 but that's a batch background job; here the user is staring at a
# spinner.
_MAX_LOGIN_RETRIES = 5

# Per-step timeouts in seconds. Generous enough for Iranian-VPS latency but
# short enough that a hung broker host doesn't pin the button forever.
_HTTP_TIMEOUT_S = 10.0

# In-process Bearer-token cache. Keyed by (broker_code, username,
# password-fingerprint) so a different password gets a clean miss (and
# therefore a fresh login + real password check). Holds (token, expires_at).
# Cleared on container restart — which is fine: the captcha cost on a
# cold UI process is the same as it always was.
#
# A 30 minute TTL is comfortably inside the typical broker JWT lifetime
# (~2h) but short enough that a revoked / rotated token is dropped before
# it can stay wedged in the cache for long.
_TOKEN_CACHE: dict[tuple[str, str, str], tuple[str, float]] = {}
_TOKEN_CACHE_TTL_S = 30 * 60


def _password_fingerprint(password: str) -> str:
    """SHA-256 of the password, first 16 hex chars. NOT a security
    construct — the password is already in process memory at this point
    — just a stable cache-key component so different passwords miss the
    cache (and therefore trigger a real login + real password check)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]


def _token_cache_get(broker_code: str, username: str, password: str) -> Optional[str]:
    key = (broker_code, username, _password_fingerprint(password))
    entry = _TOKEN_CACHE.get(key)
    if entry is None:
        return None
    token, expires_at = entry
    if time.monotonic() >= expires_at:
        _TOKEN_CACHE.pop(key, None)
        return None
    return token


def _token_cache_put(broker_code: str, username: str, password: str, token: str) -> None:
    key = (broker_code, username, _password_fingerprint(password))
    _TOKEN_CACHE[key] = (token, time.monotonic() + _TOKEN_CACHE_TTL_S)


def _token_cache_drop(broker_code: str, username: str, password: str) -> None:
    key = (broker_code, username, _password_fingerprint(password))
    _TOKEN_CACHE.pop(key, None)


def _endpoints_for(broker_code: str) -> dict[str, str]:
    """Return the captcha / login / customer_info / market_data URL set
    for a broker.

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
            "market_data": "https://mdapi.ibtrader.ir/api/v2/instruments/full",
            # GetOrders lives on the regular api host (api.ibtrader.ir),
            # NOT the api8 customer-info shard — same host the bot fires
            # NewOrder/GetOpenOrders against.
            "orders": "https://api.ibtrader.ir/api/v2/orders/GetOrders",
            # Portfolio lives on the api8 shard (same shard as customer_info)
            # — mirrors ``SellerMarket/broker_enum.py::get_endpoints_for``.
            "portfolio": (
                "https://api8.ibtrader.ir/api/portfolio/getrealsecuritypositionbydate"
            ),
        }
    # ephoenix family — same prefix shape as the bot. Note that
    # ``market_data`` is a SHARED host across the whole ephoenix family
    # (``marketdatagw.ephoenix.ir``) — no per-broker prefix there.
    domain = "ephoenix.ir"
    prefix = f"-{broker_code}."
    return {
        "captcha": f"https://identity{prefix}{domain}/api/Captcha/GetCaptcha",
        "login": f"https://identity{prefix}{domain}/api/v2/accounts/login",
        "customer_info": (
            f"https://backofficeexternal{prefix}{domain}"
            "/api/party/getcustomerinfo"
        ),
        "market_data": "https://marketdatagw.ephoenix.ir/api/v2/instruments/full",
        # GetOrders is a sibling of NewOrder/GetOpenOrders on the per-broker
        # ``api-{code}.ephoenix.ir`` host. Confirmed to accept the same
        # ``Authorization: Bearer`` token as the bot's order calls.
        "orders": f"https://api{prefix}{domain}/api/v2/orders/GetOrders",
        # Portfolio is a sibling of customer_info on the backoffice host —
        # mirrors ``SellerMarket/broker_enum.py::get_endpoints_for``.
        "portfolio": (
            f"https://backofficeexternal{prefix}{domain}"
            "/api/portfolio/getrealsecuritypositionbydate"
        ),
    }


def _ocr_base_urls(ocr_service_url: str) -> list[str]:
    """Parse the ``ocr_service_url`` setting into an ordered list of base URLs.

    Accepts a single URL or a comma/space-separated list (client-side OCR
    pool — HA plan WS1); trailing slashes are stripped. A single URL yields a
    one-element list (backward compatible).
    """
    raw = (ocr_service_url or "").replace(",", " ")
    return [part.rstrip("/") for part in raw.split() if part.strip()]


async def _solve_captcha(
    client: httpx.AsyncClient,
    ocr_service_url: str,
    captcha_byte_data: str,
) -> Optional[str]:
    """Send a captcha image to the OCR microservice and return the decoded text.

    ``ocr_service_url`` may be a single URL or a comma/space-separated list of
    endpoints; we try them in order and fail over to the next on a transport
    error (so one OCR host going down doesn't break credential verification).
    Returns ``None`` if a healthy host decoded to an empty body (ambiguous
    captcha); raises if every endpoint had a transport/HTTP error.

    Mirrors the wire contract in
    ``SellerMarket/captcha_utils.py::decode_captcha``:

    * ``POST {base}/ocr/captcha-easy-base64``
    * headers: ``Content-Type: application/json``, ``accept: text/plain``
    * body: ``{"base64": "<base64-image-string>"}``
    * response body is the decoded text in plain text, occasionally wrapped
      in JSON-style double quotes — peel them off.
    """
    bases = _ocr_base_urls(ocr_service_url)
    if not bases:
        raise httpx.HTTPError("no OCR endpoints configured")
    last_error: Optional[Exception] = None
    for base in bases:
        try:
            resp = await client.post(
                base + "/ocr/captcha-easy-base64",
                json={"base64": captcha_byte_data},
                headers={"accept": "text/plain", "Content-Type": "application/json"},
                timeout=_HTTP_TIMEOUT_S,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            last_error = exc
            logger.warning("OCR endpoint %s failed, trying next: %s", base, exc)
            continue
        text = (resp.text or "").strip()
        # Some OCR backends return ``"ABCD"`` (quoted) — peel them.
        if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        return text or None
    assert last_error is not None
    raise last_error


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


async def _get_token_with_retries(
    client: httpx.AsyncClient,
    endpoints: dict[str, str],
    username: str,
    password: str,
    ocr_service_url: str,
) -> tuple[Optional[str], Optional[str]]:
    """Drive the captcha-login retry loop. Return (token, last_error).

    Extracted from ``verify_credentials`` so both verify endpoints (creds
    + isin) can reuse the same path — captcha solves are flaky, so each
    operation gets up to ``_MAX_LOGIN_RETRIES`` attempts of its own.
    """
    last_error: Optional[str] = None
    for attempt in range(1, _MAX_LOGIN_RETRIES + 1):
        try:
            token = await _login_once(
                client, endpoints, username, password, ocr_service_url
            )
        except httpx.HTTPError as exc:
            # Transport-level failure on captcha / OCR / login.
            # Include the URL + exception class on the WARNING line so the
            # operator can tell *which* of the three hosts actually failed.
            failed_url = getattr(getattr(exc, "request", None), "url", None)
            last_error = (
                f"login attempt {attempt} failed ({type(exc).__name__}"
                f" on {failed_url}): {exc}"
            )
            logger.warning(last_error)
            continue
        if token:
            return token, None
    return None, last_error


async def _get_token(
    client: httpx.AsyncClient,
    broker_code: str,
    endpoints: dict[str, str],
    username: str,
    password: str,
    ocr_service_url: str,
    *,
    force_refresh: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """Cached wrapper around ``_get_token_with_retries``.

    Returns ``(token, last_error)``. Uses an in-process LRU keyed by
    ``(broker, username, password-fingerprint)`` so repeated UI clicks
    on Verify Credentials / Verify ISIN don't each pay the
    ~5-second captcha cost.

    Pass ``force_refresh=True`` to skip the cache (used when a
    downstream call returns 401 — the cached token has expired
    server-side and we need a fresh one).
    """
    if not force_refresh:
        cached = _token_cache_get(broker_code, username, password)
        if cached:
            logger.debug(
                "token cache HIT for %s@%s (skipping captcha+login)",
                username, broker_code,
            )
            return cached, None
    logger.debug(
        "token cache MISS for %s@%s (running captcha+login)",
        username, broker_code,
    )
    token, err = await _get_token_with_retries(
        client, endpoints, username, password, ocr_service_url
    )
    if token:
        _token_cache_put(broker_code, username, password, token)
    return token, err


async def _fetch_customer_info(
    client: httpx.AsyncClient, url: str, token: str
) -> httpx.Response:
    """GET ``customer_info`` with the given Bearer token. Caller checks
    the response status (401 → cache invalidate + retry, else
    ``raise_for_status``)."""
    return await client.get(
        url,
        headers={
            "authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            ),
        },
        timeout=_HTTP_TIMEOUT_S,
    )


async def _ephoenix_verify_credentials(
    broker_code: str,
    username: str,
    password: str,
    ocr_service_url: str,
) -> VerifyResult:
    """Verify broker credentials and return the broker-side customer info.

    See module docstring for the full flow. Uses the in-process token
    cache when possible; on a 401 from the customer-info call we drop
    the cached token and retry once with a fresh login.
    """
    endpoints = _endpoints_for(broker_code)

    async with httpx.AsyncClient() as client:
        token, last_error = await _get_token(
            client, broker_code, endpoints, username, password, ocr_service_url
        )
        if not token:
            return VerifyResult(
                ok=False,
                error=(
                    last_error
                    or "Authentication failed — check username/password "
                    f"(captcha solve gave up after {_MAX_LOGIN_RETRIES} attempts)"
                ),
            )

        # Token in hand — call getcustomerinfo. GET, not POST; the broker
        # returns 405 for POST. The user-id is read from the Bearer token.
        # If the cached token returns 401, the broker has invalidated it
        # (rotated, expired, revoked): drop our cache entry and try once
        # more with a fresh login.
        try:
            info_resp = await _fetch_customer_info(
                client, endpoints["customer_info"], token
            )
            if info_resp.status_code == 401:
                logger.info(
                    "cached token for %s@%s returned 401 — refreshing",
                    username, broker_code,
                )
                _token_cache_drop(broker_code, username, password)
                token, last_error = await _get_token(
                    client, broker_code, endpoints, username, password,
                    ocr_service_url, force_refresh=True,
                )
                if not token:
                    return VerifyResult(
                        ok=False,
                        error=(
                            last_error
                            or "Authentication failed after token refresh — "
                            "check username/password"
                        ),
                    )
                info_resp = await _fetch_customer_info(
                    client, endpoints["customer_info"], token
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


async def _ephoenix_verify_isin(
    broker_code: str,
    username: str,
    password: str,
    isin: str,
    ocr_service_url: str,
) -> IsinInfo:
    """Look up an ISIN against the broker's ``market_data`` endpoint and
    return the broker-side symbol / title / price-bounds.

    Same login flow as :func:`verify_credentials` — captcha + OCR + login
    to obtain a Bearer token — then ``POST /api/v2/instruments/full``
    with ``{"isinList": [<isin>]}`` and pluck the first record.

    Mirrors ``SellerMarket/api_client.py::get_instrument_info`` wire
    contract.
    """
    endpoints = _endpoints_for(broker_code)

    async def _do_market_data_call(client, token, *, max_attempts=3):
        """POST market_data with the given Bearer token, retrying on
        transient transport failures. Returns ``(response, error_str)``
        — exactly one is non-None."""
        last = None
        for md_attempt in range(1, max_attempts + 1):
            try:
                resp = await client.post(
                    endpoints["market_data"],
                    headers={
                        "authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36"
                        ),
                    },
                    json={"isinList": [isin]},
                    timeout=20.0,
                )
                if resp.status_code == 401:
                    # Don't burn retries on a 401 — caller decides.
                    return resp, None
                resp.raise_for_status()
                return resp, None
            except httpx.HTTPError as exc:
                failed_url = getattr(getattr(exc, "request", None), "url", None)
                last = (
                    f"market_data attempt {md_attempt} failed "
                    f"({type(exc).__name__} on {failed_url}): {exc or '<no detail>'}"
                )
                logger.warning(last)
        return None, (last or "market_data exhausted retries")

    async with httpx.AsyncClient() as client:
        token, last_error = await _get_token(
            client, broker_code, endpoints, username, password, ocr_service_url
        )
        if not token:
            return IsinInfo(
                ok=False,
                isin=isin,
                error=(
                    last_error
                    or "Authentication failed — check username/password "
                    f"(captcha solve gave up after {_MAX_LOGIN_RETRIES} attempts)"
                ),
            )

        md_resp, md_error = await _do_market_data_call(client, token)
        if md_resp is not None and md_resp.status_code == 401:
            # Cached token rejected — refresh and try once more.
            logger.info(
                "cached token for %s@%s returned 401 on market_data — refreshing",
                username, broker_code,
            )
            _token_cache_drop(broker_code, username, password)
            token, last_error = await _get_token(
                client, broker_code, endpoints, username, password,
                ocr_service_url, force_refresh=True,
            )
            if not token:
                return IsinInfo(
                    ok=False,
                    isin=isin,
                    error=(
                        last_error
                        or "Authentication failed after token refresh — "
                        "check username/password"
                    ),
                )
            md_resp, md_error = await _do_market_data_call(client, token)
        if md_resp is None:
            return IsinInfo(
                ok=False,
                isin=isin,
                error=md_error
                or "cannot reach broker market-data endpoint after 3 attempts",
            )

        try:
            instruments = md_resp.json() or []
        except ValueError:
            return IsinInfo(
                ok=False,
                isin=isin,
                error="broker returned non-JSON market data",
            )
        if not instruments:
            return IsinInfo(
                ok=False,
                isin=isin,
                error=f"No instrument found for ISIN {isin}.",
            )

        # Same nested-key shape used by the bot's get_instrument_info.
        # Be defensive about missing nested keys — surface a clear error
        # rather than letting a KeyError become a 500.
        item = instruments[0] or {}
        i = item.get("i") or {}
        t = item.get("t") or {}
        return IsinInfo(
            ok=True,
            isin=isin,
            symbol=i.get("s") or None,
            title=i.get("t") or None,
            last_price=t.get("cup"),
            min_price=t.get("minap"),
            max_price=t.get("maxap"),
            max_volume=i.get("maxeq"),
            min_volume=i.get("mineq"),
        )


# Order-history pagination. The broker caps page size; 100 keeps each page
# small enough to be reliable over Iranian-VPS latency while not making a
# year of history into hundreds of round-trips. The page cap is a runaway
# guard against a broker that returns an inconsistent ``totalRecords``.
_ORDERS_PAGE_SIZE = 100
_ORDERS_MAX_PAGES = 500
_ORDERS_HTTP_TIMEOUT_S = 30.0


async def _ephoenix_get_orders(
    broker_code: str,
    username: str,
    password: str,
    ocr_service_url: str,
    *,
    from_date: str,
    to_date: str,
    side: Optional[int] = None,
    isin: Optional[str] = None,
    include_status: Optional[list[int]] = None,
    page_size: int = _ORDERS_PAGE_SIZE,
    max_pages: int = _ORDERS_MAX_PAGES,
) -> tuple[list[dict], Optional[str]]:
    """Fetch the account's order history from the broker's ``GetOrders``.

    Reuses the same captcha→OCR→login→Bearer-token flow as
    :func:`verify_credentials` (incl. the in-process token cache and the
    401→refresh path). The endpoint accepts the same ``Authorization:
    Bearer`` token the bot uses for NewOrder/GetOpenOrders, so we do NOT
    need the browser's ``x-sessionId`` header.

    ``from_date`` / ``to_date`` are ``"YYYY/MM/DD"`` Gregorian strings
    (the broker's wire format — matches the documented request body).
    ``include_status`` defaults to ``[3]`` (fully-executed) which is the
    only state we care about for the fee report; pass ``[]`` for all
    states. Paginates ``page``/``pageSize`` until every ``totalRecords``
    row is consumed (capped at ``max_pages``).

    Returns ``(rows, error)`` — ``rows`` is the flat list of raw GetOrders
    row dicts (the caller maps them to ORM rows), ``error`` is a non-None
    operator-facing string when the fetch could not complete. On a partial
    failure mid-pagination we return the rows gathered so far AND an error
    so the caller can surface "got N rows but the broker errored on page K".
    """
    if include_status is None:
        include_status = [3]
    endpoints = _endpoints_for(broker_code)
    url = endpoints["orders"]

    def _body(page: int) -> dict:
        return {
            "page": page,
            "pageSize": page_size,
            "fromDate": from_date,
            "toDate": to_date,
            "side": side,
            "isin": isin,
            "includeStatus": include_status,
            "pamCode": None,
        }

    async def _post(client, token, page):
        return await client.post(
            url,
            headers={
                "authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                ),
            },
            json=_body(page),
            timeout=_ORDERS_HTTP_TIMEOUT_S,
        )

    rows: list[dict] = []
    async with httpx.AsyncClient() as client:
        token, last_error = await _get_token(
            client, broker_code, endpoints, username, password, ocr_service_url
        )
        if not token:
            return [], (
                last_error
                or "Authentication failed — check username/password "
                f"(captcha solve gave up after {_MAX_LOGIN_RETRIES} attempts)"
            )

        refreshed_once = False
        page = 1
        while page <= max_pages:
            try:
                resp = await _post(client, token, page)
                if resp.status_code == 401 and not refreshed_once:
                    # Cached token rejected — drop it, re-login once, retry
                    # the SAME page. Mirrors verify_credentials' 401 path.
                    logger.info(
                        "cached token for %s@%s returned 401 on GetOrders — refreshing",
                        username, broker_code,
                    )
                    _token_cache_drop(broker_code, username, password)
                    token, last_error = await _get_token(
                        client, broker_code, endpoints, username, password,
                        ocr_service_url, force_refresh=True,
                    )
                    refreshed_once = True
                    if not token:
                        return rows, (
                            last_error
                            or "Authentication failed after token refresh"
                        )
                    continue
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                failed_url = getattr(getattr(exc, "request", None), "url", None)
                err = (
                    f"GetOrders page {page} failed "
                    f"({type(exc).__name__} on {failed_url}): {exc}"
                )
                logger.warning(err)
                return rows, err

            try:
                payload = resp.json() if resp.content else {}
            except ValueError:
                return rows, "broker returned non-JSON order history"

            page_rows = payload.get("rows") or []
            rows.extend(page_rows)

            total = payload.get("totalRecords")
            if not page_rows:
                break
            if isinstance(total, int) and page * page_size >= total:
                break
            page += 1

    return rows, None


async def _ephoenix_get_holdings(
    broker_code: str,
    username: str,
    password: str,
    isin: str,
    *,
    ocr_service_url: str,
) -> int:
    """Fetch the account's whole-share holding for one ISIN from the broker's
    portfolio endpoint.

    Mirrors the bot's ``SellerMarket/api_client.py::get_holdings`` wire shape:
    ``POST {portfolio}`` with ``{"entity": true}`` and the same Bearer token
    flow as :func:`_ephoenix_get_orders` (in-process token cache + the
    401→drop-and-refresh-once path). The response's ``result`` list carries one
    item per position; ``remainVolume`` is a float like ``445608.000`` —
    truncate to whole shares (the exchange only fills integer volumes).

    The ISIN being absent from the portfolio is a VALID answer (the account
    holds nothing) → ``0``, NOT an error. Raises on auth/transport failures
    and on a broker-side ``isError`` response — the latter with the
    operator-readable Persian ``message`` surfaced verbatim.
    """
    endpoints = _endpoints_for(broker_code)
    url = endpoints["portfolio"]

    async def _post(client, token):
        return await client.post(
            url,
            headers={
                "authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                ),
            },
            # The broker requires {"entity": true} as the request body.
            json={"entity": True},
            timeout=_HTTP_TIMEOUT_S,
        )

    async with httpx.AsyncClient() as client:
        token, last_error = await _get_token(
            client, broker_code, endpoints, username, password, ocr_service_url
        )
        if not token:
            raise RuntimeError(
                last_error
                or "Authentication failed — check username/password "
                f"(captcha solve gave up after {_MAX_LOGIN_RETRIES} attempts)"
            )

        resp = await _post(client, token)
        if resp.status_code == 401:
            # Cached token rejected — drop it, re-login once, retry. Mirrors
            # the GetOrders 401 path.
            logger.info(
                "cached token for %s@%s returned 401 on portfolio — refreshing",
                username, broker_code,
            )
            _token_cache_drop(broker_code, username, password)
            token, last_error = await _get_token(
                client, broker_code, endpoints, username, password,
                ocr_service_url, force_refresh=True,
            )
            if not token:
                raise RuntimeError(
                    last_error or "Authentication failed after token refresh"
                )
            resp = await _post(client, token)
        resp.raise_for_status()

        payload = resp.json() or {}
        if payload.get("isError"):
            # Persian message — operator-readable. Surface verbatim.
            raise RuntimeError(
                payload.get("message") or "portfolio endpoint returned isError=true"
            )
        for item in payload.get("result") or []:
            if item.get("isin") == isin:
                return int(item.get("remainVolume", 0) or 0)
        # ISIN not in the portfolio → the account holds 0 shares.
        return 0


# ---------------------------------------------------------------------------
# Family routing dispatchers
# ---------------------------------------------------------------------------
#
# All 11 current brokers are the ephoenix family (the implementations above).
# A new "exir" family is dispatched to ``app.services.brokers.exir``. The
# public function names + signatures below are UNCHANGED from before the
# family split, so callers/tests that ``from app.services.broker_client import
# verify_credentials`` keep working.
#
# ``_family`` resolves a broker code to its family via the registry. The ephoenix
# bodies stay in THIS module, so tests that patch internals like
# ``broker_client._solve_captcha`` / ``broker_client._endpoints_for`` continue to
# affect the ephoenix branch as before.


# The 11 brokers that predate the DB-managed ``brokers`` table. They are the ONLY
# codes allowed to fall back to ephoenix when the registry can't resolve a family
# (cold cache / DB unavailable). Any other code — notably an Exir tenant — is NOT
# in this set, so a resolution failure SURFACES as an error instead of silently
# routing a non-ephoenix broker through the wrong adapter (CodeRabbit). Unit tests
# that don't seed the brokers table use these codes (e.g. "ayandeh").
_LEGACY_EPHOENIX_CODES = frozenset(
    {
        "gs", "bbi", "shahr", "ib", "karamad", "tejarat",
        "ebb", "hbc", "rabin", "ayandeh", "farabi",
    }
)


async def _family(broker_code: str) -> str:
    """Resolve a broker code to its family via the registry.

    On the normal path the warm DB-backed cache returns the family. If
    resolution is unavailable (cold cache / DB down / unknown code) we fall back
    to ``"ephoenix"`` ONLY for a known legacy ephoenix code — for any other code
    the error propagates so the dispatchers below return a clean per-call error
    rather than silently misrouting (e.g.) an Exir broker through ephoenix.
    """
    try:
        await registry.ensure_family_cache()
        return registry.family_of(broker_code)
    except Exception:
        if broker_code in _LEGACY_EPHOENIX_CODES:
            return "ephoenix"
        raise


async def verify_credentials(
    broker_code: str,
    username: str,
    password: str,
    ocr_service_url: str,
) -> VerifyResult:
    """Verify broker credentials, routing by broker family.

    ephoenix (the default) keeps its in-module implementation; exir is
    delegated to its adapter. Signature is identical to the pre-split public
    ``verify_credentials``.
    """
    try:
        family = await _family(broker_code)
    except Exception as exc:  # noqa: BLE001 — surface, don't misroute
        return VerifyResult(
            ok=False,
            error=f"could not resolve broker family for {broker_code!r}: {exc}",
        )
    if family == "exir":
        from app.services.brokers.exir import ExirAdapter

        return await ExirAdapter(broker_code).verify_credentials(
            username, password, ocr_service_url
        )
    return await _ephoenix_verify_credentials(
        broker_code, username, password, ocr_service_url
    )


async def verify_isin(
    broker_code: str,
    username: str,
    password: str,
    isin: str,
    ocr_service_url: str,
) -> IsinInfo:
    """Look up an instrument, routing by broker family.

    Signature is identical to the pre-split public ``verify_isin``.
    """
    try:
        family = await _family(broker_code)
    except Exception as exc:  # noqa: BLE001 — surface, don't misroute
        return IsinInfo(
            ok=False,
            error=f"could not resolve broker family for {broker_code!r}: {exc}",
        )
    if family == "exir":
        from app.services.brokers.exir import ExirAdapter

        return await ExirAdapter(broker_code).verify_isin(
            username, password, isin, ocr_service_url
        )
    return await _ephoenix_verify_isin(
        broker_code, username, password, isin, ocr_service_url
    )


async def get_orders(
    broker_code: str,
    username: str,
    password: str,
    ocr_service_url: str,
    *,
    from_date: str,
    to_date: str,
    side: Optional[int] = None,
    isin: Optional[str] = None,
    include_status: Optional[list[int]] = None,
    page_size: int = _ORDERS_PAGE_SIZE,
    max_pages: int = _ORDERS_MAX_PAGES,
) -> tuple[list[dict], Optional[str]]:
    """Fetch the account's order history, routing by broker family.

    Signature (incl. the keyword-only block) is identical to the pre-split
    public ``get_orders``.
    """
    try:
        family = await _family(broker_code)
    except Exception as exc:  # noqa: BLE001 — surface, don't misroute the sweep
        return [], f"could not resolve broker family for {broker_code!r}: {exc}"
    if family == "exir":
        from app.services.brokers.exir import ExirAdapter

        return await ExirAdapter(broker_code).get_orders(
            username,
            password,
            ocr_service_url,
            from_date=from_date,
            to_date=to_date,
            side=side,
            isin=isin,
            include_status=include_status,
            page_size=page_size,
            max_pages=max_pages,
        )
    return await _ephoenix_get_orders(
        broker_code,
        username,
        password,
        ocr_service_url,
        from_date=from_date,
        to_date=to_date,
        side=side,
        isin=isin,
        include_status=include_status,
        page_size=page_size,
        max_pages=max_pages,
    )


async def get_holdings(
    broker_code: str,
    username: str,
    password: str,
    isin: str,
    *,
    ocr_service_url: str,
) -> int:
    """Whole-share holding for one ISIN, routing by broker family.

    Powers the trade-instruction form's "Auto-sell only" holdings preview.
    Unlike the result-shaped dispatchers above this one RAISES on any failure
    (family resolution, auth, transport, broker-side error) — the route wraps
    it and degrades to a friendly "could not fetch holding". ``ocr_service_url``
    is keyword-only: it's plumbing for the captcha→OCR login both families
    need, not part of the holdings question itself.
    """
    family = await _family(broker_code)
    if family == "exir":
        from app.services.brokers.exir import ExirAdapter

        return await ExirAdapter(broker_code).get_holdings(
            username, password, isin, ocr_service_url=ocr_service_url
        )
    return await _ephoenix_get_holdings(
        broker_code, username, password, isin, ocr_service_url=ocr_service_url
    )
