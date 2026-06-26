"""OnlinePlus (Tadbir "Online+") broker-family adapter — reference tenant Hafez.

A THIRD :class:`~app.services.brokers.base.BrokerAdapter`, alongside ephoenix
and exir. OnlinePlus is the Tadbir Pardaz web-trading platform that powers Hafez
and many other Iranian brokers. Its protocol is **cookie-session auth** (NOT
``Authorization: Bearer`` and NOT exir's per-request ``X-App-N`` signature):
a successful login on the per-tenant ``api.{code}broker.ir`` host sets an
``AuthCookie_OnlineCookie`` (plus F5 cookies) that authorizes every subsequent
read. The captcha is 4 numeric digits and is solved by the dedicated OCR CNN
endpoint ``/ocr/onlineplusplatforms-base64`` (NOT the 5-digit easy endpoint).

Confirmed live (read-only spike against Hafez, account 4580090306):
  - host: web ``online.{code}broker.ir`` embeds ``var ApiBaseURl = '...'`` →
    ``api.{code}broker.ir``.
  - captcha: ``GET {api}/Web/V1/Authenticate/GetCaptchaImage/Captcha`` →
    ``{Data:{Captcha:<b64 PNG>, CaptchaKey}}``.
  - login: ``POST {api}/Web/V2/Authenticate/Login`` ``{UserName, Password,
    Captcha, CaptchaKey}`` → ``{IsSuccessfull, Data:{Token, CustomerName,
    BourseCode, ActiveSms, ActiveOtp, MustChangePassword, ...}}`` + Set-Cookie.
  - reject markers (HTTP 200, ``IsSuccessfull:false``): ``MessageCode`` =
    ``oms_1000`` wrong username/password, ``InvalidCaptcha`` wrong captcha.
  - reads: ``GET .../Accounting/Remain`` (PurchasingPower), ``GET
    .../RealtimePortfolio/Get/RealtimePortfolio`` (holdings), ``POST
    .../Order/GetOrderList/Customer/GetOrderList`` (executed orders).

Market-data (verify_isin) reuses the shared public RLC backend — the SAME
``getstockprice2`` source exir uses — so no broker login is needed there.

Phase 1 is read-only (verify + report). Order placement lives in the bot
(``SellerMarket/onlineplus_adapter.py``, Phase 2).
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Optional

import httpx

from app.services.brokers import _rlc, registry
from app.services.brokers._cookies import cookies_to_dict
from app.services.brokers.base import CredStatus, IsinInfo, VerifyResult

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 20.0

# Max captcha solve attempts per login (the OCR CNN is good but not perfect).
_MAX_CAPTCHA_ATTEMPTS = 6

# The 4-digit OnlinePlus captcha needs the dedicated CNN OCR route, NOT the
# 5-digit easy route the ephoenix/exir families use.
_OCR_PATH = "/ocr/onlineplusplatforms-base64"

# Login-failure discriminator (LIVE-confirmed on Hafez). The login is HTTP 200
# in every case; the body's ``MessageCode`` separates the cases — we key on the
# code (language-independent) not the Persian ``MessageDesc``:
#   oms_1000       → wrong username/password → INVALID_CREDENTIALS
#   InvalidCaptcha → wrong captcha (OCR miss) → retry (TRANSIENT)
# Anything else → TRANSIENT (conservative — a false INVALID would stop a good
# account from trading).
_ONLINEPLUS_MSGCODE_INVALID_CREDENTIALS = "oms_1000"
_ONLINEPLUS_MSGCODE_INVALID_CAPTCHA = "invalidcaptcha"

# Session TTL. OnlinePlus login returns no explicit validity-minutes field, so
# we use a conservative fixed lifetime and re-login on expiry / a 401.
_SESSION_TTL_S = 600.0

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Scrape the API base URL out of the web login page (``var ApiBaseURl = '...'``).
_API_BASE_RE = re.compile(r"ApiBaseURl\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)

# Module caches (cleared on process restart — re-login/re-scrape cost is the
# same as a cold start, which is acceptable).
_SESSION_CACHE: dict[tuple[str, str, str], dict] = {}
_API_BASE_CACHE: dict[str, str] = {}


def _classify_onlineplus_login(body: object) -> bool:
    """True iff an OnlinePlus login body is a high-confidence wrong-password
    reject. Pure helper — conservative: only ``MessageCode == 'oms_1000'``
    (case-insensitive) qualifies; a success body (``IsSuccessfull``) never does."""
    if not isinstance(body, dict) or body.get("IsSuccessfull"):
        return False
    code = body.get("MessageCode")
    return (
        isinstance(code, str)
        and code.strip().lower() == _ONLINEPLUS_MSGCODE_INVALID_CREDENTIALS
    )


def _is_invalid_captcha(body: object) -> bool:
    """True iff the login body is the wrong-captcha marker (retry, not reject)."""
    if not isinstance(body, dict):
        return False
    code = body.get("MessageCode")
    return (
        isinstance(code, str)
        and code.strip().lower() == _ONLINEPLUS_MSGCODE_INVALID_CAPTCHA
    )


class _OnlinePlusInvalidCredentials(Exception):
    """Internal signal: the broker positively rejected the username/password."""


def _pw_fingerprint(password: str) -> str:
    """SHA-256 of the password, first 16 hex chars — a stable cache-key
    component only (the password is already in process memory)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]


def _ocr_base_urls(ocr_service_url: str) -> list[str]:
    """Comma/space-separated OCR endpoints -> ordered list (trailing ``/``
    stripped). Client-side OCR pool with failover (HA plan WS1)."""
    raw = (ocr_service_url or "").replace(",", " ")
    return [part.rstrip("/") for part in raw.split() if part.strip()]


class OnlinePlusAdapter:
    """Adapter for the OnlinePlus / Tadbir Online+ broker family (Hafez et al.)."""

    family = "onlineplus"

    def __init__(self, code: str):
        self.code = code
        # OnlinePlus tenants don't share one host convention (Hafez =
        # hafezbroker.ir, but e.g. dnovin = dnovinbr.ir). Prefer the per-broker
        # ``base_domain`` (warm registry cache); fall back to the legacy
        # ``{code}broker.ir`` convention when it's unset. The web host embeds the
        # API base URL (scraped in _resolve_api_base); api.{...} is the fallback.
        domain = registry.base_domain_of(code)
        if domain:
            self._web_base = f"https://online.{domain}"
            self._api_convention = f"https://api.{domain}"
        else:
            self._web_base = f"https://online.{code}broker.ir"
            self._api_convention = f"https://api.{code}broker.ir"

    # -- host discovery ---------------------------------------------------

    async def _resolve_api_base(self, client: httpx.AsyncClient) -> str:
        """Return the API base URL (e.g. ``https://api.hafezbroker.ir``).

        Scrapes ``var ApiBaseURl = '...'`` from the web login page (the same
        source the official desktop client uses), caches it module-level, and
        falls back to the ``api.{code}broker.ir`` convention if the scrape
        fails. Trailing slash stripped.
        """
        cached = _API_BASE_CACHE.get(self.code)
        if cached:
            return cached
        api = self._api_convention  # base_domain-derived or {code}broker.ir
        try:
            resp = await client.get(
                f"{self._web_base}/Account/Login", timeout=_HTTP_TIMEOUT_S
            )
            m = _API_BASE_RE.search(resp.text or "")
            if m:
                api = m.group(1).strip()
        except httpx.HTTPError as exc:
            logger.warning(
                "onlineplus %s: could not scrape ApiBaseURl (%s); using convention %s",
                self.code, exc, api,
            )
        api = api.rstrip("/")
        _API_BASE_CACHE[self.code] = api
        return api

    # -- captcha / login --------------------------------------------------

    async def _solve_captcha(
        self, client: httpx.AsyncClient, ocr_service_url: str, b64: str
    ) -> str:
        """POST a base64 captcha image to the OCR microservice's ONLINEPLUS CNN
        route; return the decoded digits (quotes peeled). Fails over across the
        comma/space-separated OCR pool on a transport error."""
        bases = _ocr_base_urls(ocr_service_url)
        if not bases:
            raise httpx.HTTPError("no OCR endpoints configured")
        last_error: Optional[Exception] = None
        for base in bases:
            try:
                resp = await client.post(
                    base + _OCR_PATH,
                    json={"base64": b64},
                    headers={"accept": "text/plain", "Content-Type": "application/json"},
                    timeout=_HTTP_TIMEOUT_S,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning(
                    "onlineplus OCR endpoint %s failed, trying next: %s", base, exc
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
        """Perform a fresh OnlinePlus login on ``client``.

        Up to :data:`_MAX_CAPTCHA_ATTEMPTS` rounds of captcha-fetch → OCR →
        login. On success returns a session dict (cookies snapshot + identity +
        the resolved api base). Raises :class:`_OnlinePlusInvalidCredentials` on
        the ``oms_1000`` marker; retries on ``InvalidCaptcha``; raises
        ``RuntimeError`` if every attempt fails.
        """
        api = await self._resolve_api_base(client)
        last_description: Optional[str] = None
        for _ in range(_MAX_CAPTCHA_ATTEMPTS):
            cap_resp = await client.get(
                f"{api}/Web/V1/Authenticate/GetCaptchaImage/Captcha",
                timeout=_HTTP_TIMEOUT_S,
            )
            cap_resp.raise_for_status()
            try:
                cdata = (cap_resp.json() or {}).get("Data") or {}
                b64 = cdata["Captcha"]
                captcha_key = cdata["CaptchaKey"]
            except (ValueError, KeyError, TypeError) as exc:
                last_description = f"malformed captcha response: {exc}"
                continue

            try:
                captcha = await self._solve_captcha(client, ocr_service_url, b64)
            except httpx.HTTPError as exc:
                last_description = f"OCR error: {exc}"
                continue
            # The OnlinePlus captcha is exactly 4 numeric digits — don't waste a
            # login attempt on a malformed OCR result; refetch.
            if not (captcha.isdigit() and len(captcha) == 4):
                last_description = f"OCR returned {captcha!r} (expected 4 digits)"
                continue

            login_resp = await client.post(
                f"{api}/Web/V2/Authenticate/Login",
                json={
                    "UserName": username,
                    "Password": password,
                    "Captcha": captcha,
                    "CaptchaKey": captcha_key,
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

            data = body.get("Data") or {}
            if body.get("IsSuccessfull") and data.get("Token"):
                first = (data.get("CustomerName") or "").strip()
                return {
                    "api": api,
                    # F5 BIG-IP fronts Hafez and sets two same-name
                    # ``f5avr…_session_`` cookies → ``dict(client.cookies)``
                    # raises httpx.CookieConflict. Flatten duplicate-safe.
                    "cookies": cookies_to_dict(client.cookies.jar),
                    "customer_name": first or None,
                    "bourse": data.get("BourseCode") or None,
                    "otp_required": bool(data.get("ActiveOtp") or data.get("ActiveSms")),
                    "must_change_password": bool(data.get("MustChangePassword")),
                    "raw": body,
                }

            # Failure. Classify the marker.
            last_description = (
                body.get("MessageDesc")
                or body.get("MessageCode")
                or f"login failed (status={login_resp.status_code})"
            )
            if _classify_onlineplus_login(body):
                # Wrong username/password — stop retrying captcha.
                raise _OnlinePlusInvalidCredentials(last_description)
            # Wrong captcha (or any other ambiguous business error) → retry.
            if _is_invalid_captcha(body):
                continue
            # Unknown non-captcha failure: retry too (conservative — never treat
            # an unrecognised marker as a hard credential reject).
            continue

        raise RuntimeError(
            f"OnlinePlus login failed after {_MAX_CAPTCHA_ATTEMPTS} attempts"
            + (f": {last_description}" if last_description else "")
        )

    def _session_key(self, username: str, password: str) -> tuple[str, str, str]:
        return (self.code, username, _pw_fingerprint(password))

    def _invalidate_session(self, username: str, password: str) -> None:
        _SESSION_CACHE.pop(self._session_key(username, password), None)

    async def _session(
        self, username: str, password: str, ocr_service_url: str
    ) -> dict:
        """Return a cached logged-in session (cookies + api base) if still
        valid, else perform a fresh login and cache it."""
        key = self._session_key(username, password)
        cached = _SESSION_CACHE.get(key)
        if cached is not None and time.monotonic() < cached["expires_at"]:
            return cached
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=_HTTP_TIMEOUT_S,
            trust_env=False,
            headers={"User-Agent": _UA, "Origin": self._web_base,
                     "Referer": self._web_base + "/"},
        ) as client:
            session = await self._login(client, username, password, ocr_service_url)
        session["expires_at"] = time.monotonic() + _SESSION_TTL_S
        _SESSION_CACHE[key] = session
        return session

    def _read_client(self, session: dict) -> httpx.AsyncClient:
        """A fresh client carrying the session cookies for a cookie-auth read."""
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=_HTTP_TIMEOUT_S,
            trust_env=False,
            headers={"User-Agent": _UA, "Origin": self._web_base,
                     "Referer": self._web_base + "/"},
        )
        for name, value in session["cookies"].items():
            client.cookies.set(name, value)
        return client

    # -- BrokerAdapter contract ------------------------------------------

    async def verify_credentials(
        self, username: str, password: str, ocr_service_url: str
    ) -> VerifyResult:
        """Log in once; report success/failure with the broker-confirmed name."""
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=_HTTP_TIMEOUT_S,
                trust_env=False,
                headers={"User-Agent": _UA, "Origin": self._web_base,
                         "Referer": self._web_base + "/"},
            ) as client:
                session = await self._login(
                    client, username, password, ocr_service_url
                )
            # Credentials are valid even if the account needs a second factor /
            # a forced password change — but flag that it can't auto-trade.
            note = "login ok"
            if session.get("otp_required"):
                note = (
                    "credentials OK, but the account has SMS/OTP enabled — "
                    "not auto-tradable until disabled"
                )
            elif session.get("must_change_password"):
                note = (
                    "credentials OK, but the account must change its password — "
                    "not auto-tradable until changed"
                )
            return VerifyResult(
                ok=True,
                status=CredStatus.VALID,
                full_name=session.get("customer_name") or session.get("bourse"),
                national_id=None,
                bourse_code=session.get("bourse"),
                message=note,
            )
        except _OnlinePlusInvalidCredentials as exc:
            return VerifyResult(
                ok=False,
                status=CredStatus.INVALID_CREDENTIALS,
                error="The broker rejected this username/password.",
                message=str(exc) or None,
            )
        except Exception as exc:  # noqa: BLE001 — surface any failure to operator
            return VerifyResult(ok=False, status=CredStatus.TRANSIENT, error=str(exc))

    async def verify_isin(
        self, username: str, password: str, isin: str, ocr_service_url: str
    ) -> IsinInfo:
        """Validate an ISIN against the public RLC market-data backend.

        OnlinePlus instruments are TSE-listed and keyed by ISIN, so we look the
        code up on the SAME public ``getstockprice2`` handler the bot prices
        orders on — no login or captcha needed. ``username``/``password``/
        ``ocr_service_url`` are unused (the endpoint is public) but kept for the
        :class:`BrokerAdapter` contract.
        """
        return await _rlc.isin_info(isin)

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
        """Fetch EXECUTED orders from OnlinePlus's ``GetOrderList``.

        ``from_date``/``to_date`` arrive as Gregorian ``"YYYY/MM/DD"`` (the
        dispatcher passes ``date.strftime("%Y/%m/%d")``) — OnlinePlus wants
        Gregorian ``"YYYY-MM-DD"``, so we just swap the separator (no Jalali
        conversion on the request; the RESPONSE dates are Jalali and are parsed
        by the mapper). ``OrderState:"1"`` returns executed orders; we keep only
        rows that actually traded (``ExcutedAmount > 0``) so a non-filled row
        can't reach the fee report. ``include_status`` other than ``{3}`` (the
        canonical "filled") and a ``side`` filter are rejected up front (mirrors
        the exir adapter). On a non-200 the session is dropped and the
        login+fetch retried once.
        """
        if include_status is not None and set(include_status) != {3}:
            return [], (
                "onlineplus adapter supports filled orders (status 3) only; "
                f"got include_status={include_status}"
            )
        if side is not None:
            return [], "onlineplus adapter does not support a side filter"

        from_g = (from_date or "").replace("/", "-")
        to_g = (to_date or "").replace("/", "-")
        path = "/Web/V1/Order/GetOrderList/Customer/GetOrderList"

        def _body(page_index: int) -> dict:
            return {
                "FromDate": from_g,
                "ToDate": to_g,
                "OrderState": "1",
                "PageIndex": page_index,
                "PageSize": page_size,
            }

        try:
            last_err: Optional[str] = None
            for _attempt in range(2):  # one retry with a fresh login on failure
                session = await self._session(username, password, ocr_service_url)
                api = session["api"]
                rows: list[dict] = []
                ok = True
                async with self._read_client(session) as client:
                    page = 0
                    while page < max_pages:
                        resp = await client.post(api + path, json=_body(page))
                        if resp.status_code != 200:
                            last_err = (
                                f"onlineplus GetOrderList HTTP {resp.status_code}: "
                                f"{resp.text[:200]}"
                            )
                            ok = False
                            break
                        data = (resp.json() or {}).get("Data") or {}
                        page_rows = data.get("Result") or []
                        rows.extend(page_rows)
                        total = data.get("TotalRecord")
                        if not page_rows:
                            break
                        if isinstance(total, int) and (page + 1) * page_size >= total:
                            break
                        page += 1
                if ok:
                    rows = [r for r in rows if _executed_qty(r) > 0]
                    if isin:
                        rows = [r for r in rows if _row_isin(r) == isin]
                    return rows, None
                # Non-200: the cached session may be dead — drop + retry once.
                self._invalidate_session(username, password)
            return [], last_err
        except Exception as exc:  # noqa: BLE001
            logger.warning("onlineplus get_orders failed: %s", exc)
            return [], f"onlineplus error: {exc}"

    async def get_holdings(
        self, username: str, password: str, isin: str, *, ocr_service_url: str
    ) -> int:
        """Whole-share holding for ``isin`` via ``RealtimePortfolio``.

        Reads ``RemainQuantity`` for the row whose ``SymbolISIN`` (capitalised
        ``SymbolIsin`` variant accepted) equals ``isin``. The ISIN being absent
        is a VALID answer (the account holds nothing) → ``0``. On a non-200 the
        session is dropped and the login+fetch retried once; a second failure
        RAISES (the dispatcher contract is raise-on-failure)."""
        path = (
            "/Web/V1/RealtimePortfolio/Get/RealtimePortfolio"
            "?GetJustHasRemain=true&EndDate=undefined&BasedOnLastPositivePeriod=true"
            "&ActiveSymbolsStartDate=&ActiveSymbolsEndDate="
        )
        last_err: Optional[str] = None
        for _attempt in range(2):
            session = await self._session(username, password, ocr_service_url)
            api = session["api"]
            async with self._read_client(session) as client:
                resp = await client.get(api + path)
            if resp.status_code == 200:
                rows = (resp.json() or {}).get("Data") or []
                for row in rows:
                    if _row_isin(row) == isin:
                        return int(row.get("RemainQuantity") or 0)
                return 0
            last_err = (
                f"onlineplus RealtimePortfolio HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
            self._invalidate_session(username, password)
        raise RuntimeError(last_err or "onlineplus RealtimePortfolio failed")


def _executed_qty(row: object) -> int:
    """Executed quantity of a GetOrderList row (``ExcutedAmount`` — the broker's
    spelling; ``ExecutedAmount`` accepted defensively), 0 on junk/None."""
    if not isinstance(row, dict):
        return 0
    val = row.get("ExcutedAmount")
    if val is None:
        val = row.get("ExecutedAmount")
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return 0


def _row_isin(row: object) -> Optional[str]:
    """ISIN of an OnlinePlus row, probing the casings the platform mixes
    (``SymbolIsin`` in GetOrderList, ``SymbolISIN`` in RealtimePortfolio)."""
    if not isinstance(row, dict):
        return None
    return row.get("SymbolIsin") or row.get("SymbolISIN")
