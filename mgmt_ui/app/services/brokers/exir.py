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
from app.services.brokers.base import IsinInfo, VerifyResult
from app.services.brokers.exir_token import build_app_n

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 20.0

# Max captcha solve attempts per login. The OCR service is good but not
# perfect; a handful of retries covers the occasional ambiguous image.
_MAX_CAPTCHA_ATTEMPTS = 6

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
        decoded text (quotes peeled). Mirrors the shared OCR wire contract."""
        url = ocr_service_url.rstrip("/") + "/ocr/captcha-easy-base64"
        resp = await client.post(
            url,
            json={"base64": b64},
            headers={"accept": "text/plain", "Content-Type": "application/json"},
            timeout=_HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        return (resp.text or "").strip().strip('"')

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
                # captcha loop (a wrong-creds error will simply exhaust the
                # attempts and surface the description below).
                last_description = (
                    body.get("description")
                    or body.get("message")
                    or f"login failed (status={login_resp.status_code})"
                )
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
            follow_redirects=True, timeout=_HTTP_TIMEOUT_S
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
                follow_redirects=True, timeout=_HTTP_TIMEOUT_S
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
                full_name=full_name,
                national_id=None,
                bourse_code=bourse,
                message="login ok",
            )
        except Exception as exc:  # noqa: BLE001 — surface any failure to operator
            return VerifyResult(ok=False, error=str(exc))

    async def verify_isin(
        self, username: str, password: str, isin: str, ocr_service_url: str
    ) -> IsinInfo:
        """Exir verification is ISIN-based; we do not fetch instrument
        metadata in Phase 1, so no broker call (and no login) is needed."""
        return IsinInfo(
            ok=True,
            isin=isin,
            symbol=isin,
            message=(
                "Exir verification is ISIN-based; instrument metadata not "
                "fetched in Phase 1."
            ),
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
        """Fetch filled orders from Exir's ``orderbookReport``.

        ``from_date``/``to_date`` arrive as Gregorian ``"YYYY/MM/DD"`` (the
        dispatcher passes ``date.strftime("%Y/%m/%d")``); they are converted to
        Jalali for the wire. Phase 1 supports FILLED orders only (status 3) —
        what the bot report needs, and what lets the mapper safely stamp
        ``state=3``. A request that excludes 3 returns an explicit error rather
        than silently fetching another status and mis-labeling it as filled.
        If ``isin`` is given, rows are filtered client-side on ``insMaxLCode``.
        On a non-200 (e.g. the broker expired our session) the cached session is
        dropped and the login+fetch is retried once before giving up.
        """
        # Phase-1 contract: filled-only. Reject a non-3 request loudly instead
        # of fetching it and stamping state=3 (which would corrupt the report).
        if include_status is not None and 3 not in include_status:
            return [], (
                "exir adapter (Phase 1) supports filled orders (status 3) only; "
                f"got include_status={include_status}"
            )
        try:
            jstart = gregorian_str_to_jalali_str(from_date)
            jend = gregorian_str_to_jalali_str(to_date)
            path = (
                "/api/v1/user/orderbookReport?size=1000"
                f"&startDate={jstart}"
                "&mmtpTypeId=null"
                f"&endDate={jend}"
                "&orderStatusId=3"
            )

            last_err: Optional[str] = None
            for attempt in range(2):  # one retry with a fresh login on failure
                session = await self._session(username, password, ocr_service_url)
                nt = session["nt"]
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=_HTTP_TIMEOUT_S
                ) as client:
                    for name, value in session["cookies"].items():
                        client.cookies.set(name, value)
                    resp = await self._signed_get(client, nt, path)

                if resp.status_code == 200:
                    rows = resp.json().get("result") or []
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
