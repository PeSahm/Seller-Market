"""Exir / Rayan HamAfza broker-family adapter.

A second :class:`~app.services.brokers.base.BrokerAdapter` implementation,
alongside the ephoenix one. Exir's web platform speaks a *cookies + per-request
``X-App-N`` signature* protocol (NOT ``Authorization: Bearer``), with **Jalali**
dates on the wire. This module is self-contained: it talks to the broker over
``httpx.AsyncClient`` directly and does NOT import private helpers from
``broker_client`` (the captcha/fingerprint helpers are reimplemented locally to
keep the two families independent — same rationale as the ephoenix split).

The confirmed live wire shape is recorded in
``SellerMarket/scratch/EXIR_FINDINGS.md`` (Phase-0 spike, 2026-06-02). Phase 1
is read-only: credential verify + ``orderbookReport`` for the bot report. No
order placement here.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import time
from typing import Optional

import httpx

from app.services.brokers._jalali import gregorian_str_to_jalali_str
from app.services.brokers._rlc import rlc_instrument as _rlc_instrument
from app.services.brokers.base import CredStatus, IsinInfo, VerifyResult
from app.services.brokers.exir_token import build_app_n

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 20.0

# The public RLC / Tadbir market-data lookup (``getstockprice2``) lives in
# ``app.services.brokers._rlc`` now (shared with the onlineplus family). It is
# imported above as ``_rlc_instrument`` so the existing patch target
# ``exir._rlc_instrument`` keeps working unchanged in tests.


def _traded_qty(row: object) -> int:
    """Executed quantity of an ``orderbookReport`` row (``tradedQuantity``),
    0 on junk/None. Used to keep only orders that ACTUALLY TRADED, since Exir's
    on-wire order status is unreliable (see :meth:`ExirAdapter.get_orders`)."""
    if not isinstance(row, dict):
        return 0
    try:
        return int(float(row.get("tradedQuantity")))
    except (TypeError, ValueError):
        return 0

# Max captcha solve attempts per login. The OCR service is good but not
# perfect; a handful of retries covers the occasional ambiguous image.
_MAX_CAPTCHA_ATTEMPTS = 6

# Exir login-failure discriminator (LIVE-confirmed on khobregan — see
# SellerMarket/scratch/CRED_STATUS_FINDINGS.md). The error body carries a numeric
# ``errorCode``:
#   40037 (HTTP 403) → wrong username/password → INVALID_CREDENTIALS
#   9002  (HTTP 401) → wrong captcha           → retry (TRANSIENT)
# We key on the numeric code (language-independent) rather than the Persian
# ``description`` (which has a trailing space + yeh-spelling variants).
_EXIR_ERRCODE_INVALID_CREDENTIALS = 40037


def _classify_exir_login(body: object) -> bool:
    """Return True iff an exir login body is a high-confidence wrong-password
    reject. Pure helper — conservative: only ``errorCode == 40037`` qualifies."""
    return (
        isinstance(body, dict)
        and body.get("errorCode") == _EXIR_ERRCODE_INVALID_CREDENTIALS
    )


class _ExirInvalidCredentials(Exception):
    """Internal signal: the broker positively rejected the username/password."""

# Skew subtracted from the broker-reported ``validity`` (minutes) so we re-login
# a little before the session actually expires. Clamped so a tiny / missing
# validity still yields a usable, non-negative TTL.
_TTL_SKEW_S = 60.0
_TTL_MIN_S = 60.0

# Session cache: (code, username, pw_fingerprint) -> session dict.
# Cleared on process restart (re-login cost == cold-start cost; acceptable).
_SESSION_CACHE: dict[tuple[str, str, str], dict] = {}


def _pw_fingerprint(password: str) -> str:
    """SHA-256 of the password, first 16 hex chars — a stable cache-key
    component only (NOT a security construct; the password is already in
    process memory). Mirrors ``broker_client._password_fingerprint`` but is
    reimplemented locally so this module stays independent."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]


def _ocr_base_urls(ocr_service_url: str) -> list[str]:
    """Comma/space-separated OCR endpoints -> ordered list (trailing ``/``
    stripped). Reimplemented locally to keep the exir family independent of
    ``broker_client`` (HA plan WS1: client-side OCR pool with failover)."""
    raw = (ocr_service_url or "").replace(",", " ")
    return [part.rstrip("/") for part in raw.split() if part.strip()]


class ExirAdapter:
    """Adapter for the Exir / Rayan HamAfza broker family."""

    family = "exir"

    def __init__(self, code: str):
        self.code = code
        self.base = f"https://{code}.exirbroker.com"

    # -- captcha / login --------------------------------------------------

    async def _solve_captcha(
        self, client: httpx.AsyncClient, ocr_service_url: str, b64: str
    ) -> str:
        """POST a base64 captcha image to the OCR microservice; return the
        decoded text (quotes peeled). ``ocr_service_url`` may list several
        endpoints (comma/space-separated) — we fail over between them on a
        transport error. Mirrors the shared OCR wire contract."""
        bases = _ocr_base_urls(ocr_service_url)
        if not bases:
            raise httpx.HTTPError("no OCR endpoints configured")
        last_error: Optional[Exception] = None
        for base in bases:
            try:
                resp = await client.post(
                    base + "/ocr/captcha-easy-base64",
                    json={"base64": b64},
                    headers={"accept": "text/plain", "Content-Type": "application/json"},
                    timeout=_HTTP_TIMEOUT_S,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning(
                    "exir OCR endpoint %s failed, trying next: %s", base, exc
                )
                continue
            return (resp.text or "").strip().strip('"')
        assert last_error is not None
        raise last_error

    async def _login(
        self,
        client: httpx.AsyncClient,
        username: str,
        password: str,
        ocr_service_url: str,
    ) -> dict:
        """Perform a fresh Exir login on ``client``.

        Flow: ``GET /exir`` (seeds ``cookiesession1``), then up to
        :data:`_MAX_CAPTCHA_ATTEMPTS` rounds of captcha-fetch -> OCR -> login.
        Returns a session dict on success; raises ``RuntimeError`` if every
        attempt fails (with the broker's last ``description`` if any).
        """
        # 1. Seed the session cookie.
        await client.get(f"{self.base}/exir", timeout=_HTTP_TIMEOUT_S)

        last_description: Optional[str] = None
        for _ in range(_MAX_CAPTCHA_ATTEMPTS):
            # 2. Fetch a captcha image (JPEG bytes). The response may carry a
            #    ``client_login_id`` header that must be echoed back as a
            #    cookie on subsequent requests.
            cap_resp = await client.get(
                f"{self.base}/captcha", timeout=_HTTP_TIMEOUT_S
            )
            cap_resp.raise_for_status()
            client_login_id = cap_resp.headers.get("client_login_id")
            if client_login_id:
                client.cookies.set("client_login_id", client_login_id)

            b64 = base64.b64encode(cap_resp.content).decode()
            try:
                captcha = await self._solve_captcha(client, ocr_service_url, b64)
            except httpx.HTTPError as exc:
                last_description = f"OCR error: {exc}"
                continue

            # The broker captcha is exactly 5 numeric digits — if OCR returned
            # anything else, don't waste a login attempt; refetch.
            if not (captcha.isdigit() and len(captcha) == 5):
                last_description = f"OCR returned {captcha!r} (expected 5 digits)"
                continue

            # 3. Attempt login. captcha is a JSON NUMBER, not a string.
            login_resp = await client.post(
                f"{self.base}/api/v2/login",
                json={
                    "username": username,
                    "password": password,
                    "captcha": int(captcha),
                    "otp": "",
                },
                timeout=_HTTP_TIMEOUT_S,
            )
            try:
                body = login_resp.json() if login_resp.content else {}
            except ValueError:
                last_description = (
                    f"login returned non-JSON (status={login_resp.status_code})"
                )
                continue

            if body.get("type") == "error" or not body.get("nt"):
                # Wrong captcha / rejected creds / business error — retry the
                # captcha loop (a wrong-captcha error will simply exhaust the
                # attempts and surface the description below).
                last_description = (
                    body.get("description")
                    or body.get("message")
                    or f"login failed (status={login_resp.status_code})"
                )
                # errorCode 40037 = wrong username/password → short-circuit (no
                # point retrying captcha on a known-bad password) so the caller
                # classifies it INVALID_CREDENTIALS. Wrong captcha (9002) retries.
                if _classify_exir_login(body):
                    raise _ExirInvalidCredentials(last_description)
                continue

            # Success.
            account_list = body.get("accountNumberList") or [{}]
            bourse = body.get("bourseAccountName") or (
                account_list[0].get("bourseAccountName")
            )
            return {
                "cookies": dict(client.cookies),
                "nt": body["nt"],
                "authToken": body.get("authToken"),
                "bourse": bourse,
                "validity": body.get("validity"),
                "first": body.get("firstName"),
                "last": body.get("lastName"),
                "raw": body,
            }

        raise RuntimeError(
            f"Exir login failed after {_MAX_CAPTCHA_ATTEMPTS} attempts"
            + (f": {last_description}" if last_description else "")
        )

    def _session_key(self, username: str, password: str) -> tuple[str, str, str]:
        """Cache key for a logged-in session: (code, username, pw-fingerprint)."""
        return (self.code, username, _pw_fingerprint(password))

    def _invalidate_session(self, username: str, password: str) -> None:
        """Drop the cached session so the next call re-logs in — used when the
        broker has invalidated the session server-side and our reads start
        failing (otherwise a dead session would wedge reads until the TTL)."""
        _SESSION_CACHE.pop(self._session_key(username, password), None)

    async def _session(
        self, username: str, password: str, ocr_service_url: str
    ) -> dict:
        """Return a cached session (cookies + nt + bourse + ...) if still
        valid, else perform a fresh login and cache it.

        The cached dict carries everything a caller needs to build a fresh
        ``AsyncClient`` (set ``cookies`` from it) and sign requests (``nt``).
        """
        key = self._session_key(username, password)
        cached = _SESSION_CACHE.get(key)
        if cached is not None and time.monotonic() < cached["expires_at"]:
            return cached

        # Fresh login on a throwaway client (cookies are snapshotted into the
        # returned session dict, so the client itself need not survive).
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_HTTP_TIMEOUT_S, trust_env=False
        ) as client:
            session = await self._login(client, username, password, ocr_service_url)

        validity_min = session.get("validity")
        if isinstance(validity_min, (int, float)) and validity_min > 0:
            ttl = max(_TTL_MIN_S, validity_min * 60.0 - _TTL_SKEW_S)
        else:
            ttl = _TTL_MIN_S
        session["expires_at"] = time.monotonic() + ttl
        _SESSION_CACHE[key] = session
        return session

    # -- signed reads -----------------------------------------------------

    async def _signed_get(
        self, client: httpx.AsyncClient, nt: str, path_with_query: str
    ) -> httpx.Response:
        """GET ``path_with_query`` with the per-request ``X-App-N`` signature.

        The signature is recomputed here (UTC, full path+query) immediately
        before the call so it reflects the current second.
        """
        headers = {
            "X-App-N": build_app_n(nt, path_with_query),
            "Accept": "application/json",
        }
        return await client.get(
            f"{self.base}{path_with_query}",
            headers=headers,
            timeout=_HTTP_TIMEOUT_S,
        )

    # -- BrokerAdapter contract ------------------------------------------

    async def verify_credentials(
        self, username: str, password: str, ocr_service_url: str
    ) -> VerifyResult:
        """Log in once; report success/failure. Uses one fresh client."""
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=_HTTP_TIMEOUT_S, trust_env=False
            ) as client:
                session = await self._login(
                    client, username, password, ocr_service_url
                )
            first = (session.get("first") or "").strip()
            last = (session.get("last") or "").strip()
            bourse = session.get("bourse")
            full_name = (f"{first} {last}".strip()) or bourse
            return VerifyResult(
                ok=True,
                status=CredStatus.VALID,
                full_name=full_name,
                national_id=None,
                bourse_code=bourse,
                message="login ok",
            )
        except _ExirInvalidCredentials as exc:
            return VerifyResult(
                ok=False,
                status=CredStatus.INVALID_CREDENTIALS,
                error="The broker rejected this username/password.",
                message=str(exc) or None,
            )
        except Exception as exc:  # noqa: BLE001 — surface any failure to operator
            # Inconclusive (captcha exhausted, OCR/broker down, business error):
            # TRANSIENT — never auto-mark a customer invalid on an ambiguous fail.
            return VerifyResult(ok=False, status=CredStatus.TRANSIENT, error=str(exc))

    async def verify_isin(
        self, username: str, password: str, isin: str, ocr_service_url: str
    ) -> IsinInfo:
        """Validate an ISIN against the public RLC market-data backend.

        Exir/RLC instruments are keyed by ISIN (``insMaxLCode``), so we look the
        code up on the SAME public ``getstockprice2`` handler the bot prices Exir
        orders on — no login or captcha needed. On a hit we return the
        broker-confirmed symbol, Persian name, price band (ceiling/floor), last
        price and max order qty. An unknown ISIN (or an unreachable backend)
        returns ``ok=False`` so a typo'd code can never look verified.

        ``username``/``password``/``ocr_service_url`` are unused (the market-data
        endpoint is public) but kept for the :class:`BrokerAdapter` contract.
        """
        isin = (isin or "").strip()
        if not isin:
            return IsinInfo(ok=False, isin=isin, error="No ISIN provided.")
        try:
            row = await _rlc_instrument(isin)
        except Exception as exc:  # noqa: BLE001 — never raise out of verify
            # ``error`` (not ``message``) — the verify partial renders ``.error``
            # for a failed lookup.
            return IsinInfo(
                ok=False,
                isin=isin,
                error=f"Could not reach market data to validate the ISIN: {exc}",
            )
        if row is None:
            return IsinInfo(
                ok=False,
                isin=isin,
                error="ISIN not found in market data — check the code.",
            )

        def _num(*keys: str) -> Optional[float]:
            for k in keys:
                try:
                    val = float(row.get(k))
                except (TypeError, ValueError):
                    continue
                if val > 0:
                    return val
            return None

        symbol = str(row.get("sf")).strip() or None if row.get("sf") is not None else None
        title = str(row.get("cn")).strip() or None if row.get("cn") is not None else None
        mxqo = _num("mxqo")
        return IsinInfo(
            ok=True,
            isin=isin,
            symbol=symbol,
            title=title,
            last_price=_num("ltp", "cp", "pcp"),
            min_price=_num("lap"),
            max_price=_num("hap"),
            max_volume=int(mxqo) if mxqo else None,
            message="Instrument confirmed via market data.",
        )

    async def get_orders(
        self,
        username: str,
        password: str,
        ocr_service_url: str,
        *,
        from_date: str,
        to_date: str,
        side: Optional[int] = None,
        isin: Optional[str] = None,
        include_status: Optional[list[int]] = None,
        page_size: int = 100,
        max_pages: int = 500,
    ) -> tuple[list[dict], Optional[str]]:
        """Fetch EXECUTED orders from Exir's ``orderbookReport``.

        ``from_date``/``to_date`` arrive as Gregorian ``"YYYY/MM/DD"`` (the
        dispatcher passes ``date.strftime("%Y/%m/%d")``); they are converted to
        Jalali for the wire.

        **Status taxonomy (learned the hard way — 2026-06):** the Phase-0 spike
        GUESSED ``orderStatusId=3`` meant "filled", but live that filter returns
        NOTHING — executed orders carry ``mmtpOrderStatusName == "انجام کلي"``
        (fully executed) and are NOT returned by ``orderStatusId=3`` (an order is
        only briefly status 3 on its own trade day, then settles to status 4),
        so the old query silently fetched ZERO historical fills and exir trades
        never reached ``broker_orders`` / the fee report. Rather than re-guess a
        status code, we fetch EVERY order in the date range (no
        ``orderStatusId`` filter) and keep only the rows that ACTUALLY TRADED
        (``tradedQuantity > 0``) — full fills, partial fills, and
        partial-fill-then-cancel all count for the fee report. The mapper stamps
        ``state=3`` (our canonical "filled") on what we return.

        ``include_status`` is the mgmt-side canonical filter (the dispatcher
        passes ``[3]`` = filled); a request for anything other than ``{3}`` is
        rejected, and ``side`` filtering is unsupported. If ``isin`` is given,
        rows are filtered client-side on ``insMaxLCode``. On a non-200 (e.g. the
        broker expired our session) the cached session is dropped and the
        login+fetch is retried once before giving up.
        """
        # Contract: our canonical "filled" only (state 3), no side filter. Reject
        # anything else up front so the mapper's stamped state=3 can't mis-label.
        if include_status is not None and set(include_status) != {3}:
            return [], (
                "exir adapter supports filled orders (status 3) only; "
                f"got include_status={include_status}"
            )
        if side is not None:
            return [], "exir adapter does not support a side filter"
        # Upper bound on a single fetch — the report has no pagination, so a very
        # wide range on a heavy account could truncate; we WARN (never silently
        # drop) if the cap is hit so the operator can narrow the range.
        size = 5000
        try:
            jstart = gregorian_str_to_jalali_str(from_date)
            jend = gregorian_str_to_jalali_str(to_date)
            path = (
                f"/api/v1/user/orderbookReport?size={size}"
                f"&startDate={jstart}"
                "&mmtpTypeId=null"
                f"&endDate={jend}"
            )

            last_err: Optional[str] = None
            for _attempt in range(2):  # one retry with a fresh login on failure
                session = await self._session(username, password, ocr_service_url)
                nt = session["nt"]
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=_HTTP_TIMEOUT_S, trust_env=False
                ) as client:
                    for name, value in session["cookies"].items():
                        client.cookies.set(name, value)
                    resp = await self._signed_get(client, nt, path)

                if resp.status_code == 200:
                    raw = resp.json().get("result") or []
                    if len(raw) >= size:
                        # Log the broker CODE + range, not the account username
                        # (a customer identifier), on this routine path.
                        logger.warning(
                            "exir orderbookReport hit the %d-row cap for broker "
                            "%s (%s..%s) — results may be truncated; use a "
                            "narrower date range",
                            size, self.code, from_date, to_date,
                        )
                    # Keep only orders that actually traded (any wire status).
                    rows = [r for r in raw if _traded_qty(r) > 0]
                    if isin:
                        rows = [r for r in rows if r.get("insMaxLCode") == isin]
                    return rows, None

                # Non-200: the cached session may be dead (server-side logout /
                # rotation). Drop it so the retry re-logs in; the ephoenix sibling
                # does the same on a 401.
                last_err = (
                    f"exir orderbookReport HTTP {resp.status_code}: {resp.text[:200]}"
                )
                self._invalidate_session(username, password)

            return [], last_err
        except Exception as exc:  # noqa: BLE001
            logger.warning("exir get_orders failed: %s", exc)
            return [], f"exir error: {exc}"

    async def get_holdings(
        self, username: str, password: str, isin: str, *, ocr_service_url: str
    ) -> int:
        """Whole-share holding for ``isin`` via ``GET /api/v1/user/portfoReport``.

        Reads the SAME quantity fields the bot adapter does
        (``SellerMarket/exir_adapter.py::_holdings``): ``asset`` first,
        ``remainQty`` as the fallback; the instrument key is ``insMaxLcode``
        (== ISIN, with the capitalised ``insMaxLCode`` variant accepted
        defensively). The ISIN being absent from the portfolio is a VALID
        answer (the account holds nothing) → ``0``, NOT an error.

        On a non-200 the cached session is dropped and the login+fetch is
        retried once (same recovery as :meth:`get_orders`); a second failure
        RAISES — the ``get_holdings`` dispatcher contract is raise-on-failure,
        unlike the result-shaped reads above.
        """
        path = "/api/v1/user/portfoReport"
        last_err: Optional[str] = None
        for _attempt in range(2):  # one retry with a fresh login on failure
            session = await self._session(username, password, ocr_service_url)
            nt = session["nt"]
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=_HTTP_TIMEOUT_S, trust_env=False
            ) as client:
                for name, value in session["cookies"].items():
                    client.cookies.set(name, value)
                resp = await self._signed_get(client, nt, path)

            if resp.status_code == 200:
                rows = (resp.json() or {}).get("result") or []
                for row in rows:
                    code = row.get("insMaxLcode") or row.get("insMaxLCode")
                    if code == isin:
                        qty = row.get("asset")
                        if qty is None:
                            qty = row.get("remainQty")
                        return int(qty or 0)
                return 0

            # Non-200: the cached session may be dead (server-side logout /
            # rotation). Drop it so the retry re-logs in.
            last_err = (
                f"exir portfoReport HTTP {resp.status_code}: {resp.text[:200]}"
            )
            self._invalidate_session(username, password)

        raise RuntimeError(last_err or "exir portfoReport failed")
