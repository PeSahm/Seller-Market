"""Mofid / Orbis (easytrader.ir) broker-family adapter — the 4th family.

A FOURTH :class:`~app.services.brokers.base.BrokerAdapter`, alongside ephoenix,
exir, and onlineplus. Mofid is a SINGLE broker (no per-tenant subdomain). Its
protocol is **OAuth2 Authorization-Code + PKCE** against ``login.emofid.com``
(an HTML-form scrape with an OPTIONAL BotDetect captcha that appears on retry),
then ``Authorization: Bearer`` on the ``api-mts.orbis.easytrader.ir`` JSON API.

Confirmed live (read-only Phase-0 spike, account 4580090306 — see
``SellerMarket/scratch/MOFID_FINDINGS.md``):
  - OAuth: ``GET login.emofid.com/connect/authorize/callback`` (PKCE) → ``/Login``
    HTML form → ``POST`` creds → ``/connect/authorize`` → ``auth-callback?code=`` →
    ``POST /connect/token`` → ``{access_token, expires_in:43200}``.
  - captcha: BotDetect on the login page, solved by ``/ocr/mofid-orbis-base64``.
  - reject markers (HTML ``validation-summary-errors``): wrong creds
    ``نام کاربری یا کلمه عبور نادرست است``; captcha ``کد امنیتی…``.
  - reads: ``GET /easy/api/account/user-info`` (bourseCode), ``GET /core/api/money/``
    (buyPower), ``GET /core/api/portfolio/true`` (holdings), ``GET /core/api/order``
    (executed orders — recent-only, NO date-range param).

Market-data (verify_isin) reuses the shared public RLC backend (Mofid is
Tadbir-based). Phase-1 here is read-only (verify + report); order placement lives
in the bot (``SellerMarket/mofid_adapter.py`` + ``mofid_firer.py``).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import time
from typing import Optional
from urllib.parse import urljoin

import httpx

from app.services.brokers import _rlc
from app.services.brokers.base import CredStatus, IsinInfo, VerifyResult

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 20.0
_MAX_CAPTCHA_ATTEMPTS = 6

_API_HOST = "https://api-mts.orbis.easytrader.ir"
_OAUTH_HOST = "https://login.emofid.com"
_REDIRECT_URI = "https://d.easytrader.ir/auth-callback"
_REFERER = "https://d.easytrader.ir/"
_CLIENT_ID = "easy_pkce"
_SCOPE = "easy2_api mts_api openid profile"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# The BotDetect captcha uses the dedicated mofid-orbis OCR route (alphanumeric;
# do NOT add the onlineplus 4-digit guard).
_OCR_PATH = "/ocr/mofid-orbis-base64"
# OAuth token TTL is ~12h; cache the session, re-login on a 401 / expiry.
_SESSION_TTL_S = 600.0
_PKCE_BYTES = 72

# Reject markers (HTML validation-summary text). Key on the Persian markers (the
# only discriminator the page gives). Conservative: ONLY the wrong-creds marker →
# INVALID; captcha markers → retry; anything else → retry (TRANSIENT if unresolved).
_MARK_INVALID_CREDENTIALS = "نام کاربری یا کلمه عبور نادرست است"
_MARK_CAPTCHA_REQUIRED = "کد امنیتی را وارد کنید"
_MARK_WRONG_CAPTCHA = "کد امنیتی اشتباه است"

_TOKEN_FIELD_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]*)"'
)
_CAPTCHA_IMG_RE = re.compile(r'id="OLoginCaptcha_CaptchaImage"[^>]*src="([^"]+)"')
_CODE_RE = re.compile(r"[?&]code=([^&]+)")
_BDC_FIELDS = (
    "BDC_VCID_OLoginCaptcha",
    "BDC_BackWorkaround_OLoginCaptcha",
    "BDC_Hs_OLoginCaptcha",
    "BDC_SP_OLoginCaptcha",
)

# Module session cache keyed (code, user, pw_fingerprint) → {"token","api","expires_at"}.
_SESSION_CACHE: dict = {}


class _MofidInvalidCredentials(Exception):
    """Internal signal: the broker positively rejected the username/password."""


def _pw_fingerprint(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]


def _ocr_base_urls(ocr_service_url: str) -> list[str]:
    raw = (ocr_service_url or "").replace(",", " ")
    return [part.rstrip("/") for part in raw.split() if part.strip()]


def _form_field(html: str, name: str) -> str:
    m = re.search(r'name="' + re.escape(name) + r'"[^>]*value="([^"]*)"', html or "")
    return m.group(1) if m else ""


def _classify_mofid_login(html: object) -> Optional[str]:
    """Classify a Mofid login-page reject. Returns ``"invalid_credentials"`` |
    ``"captcha_required"`` | ``"wrong_captcha"`` | ``None``. Conservative: the
    creds marker is checked FIRST (it's only shown once the captcha passes)."""
    if not isinstance(html, str):
        return None
    if _MARK_INVALID_CREDENTIALS in html:
        return "invalid_credentials"
    if _MARK_WRONG_CAPTCHA in html:
        return "wrong_captcha"
    if _MARK_CAPTCHA_REQUIRED in html:
        return "captcha_required"
    return None


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(_PKCE_BYTES)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


class MofidAdapter:
    """Adapter for the Mofid / Orbis broker family (single broker, no tenant)."""

    family = "mofid"

    def __init__(self, code: str):
        self.code = code

    # -- captcha / login ---------------------------------------------------
    async def _solve_captcha(
        self, client: httpx.AsyncClient, ocr_service_url: str, b64: str
    ) -> str:
        """POST a base64 BotDetect image to the OCR pool's mofid-orbis route."""
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
                logger.warning("mofid OCR endpoint %s failed, trying next: %s", base, exc)
                continue
            return (resp.text or "").strip().strip('"')
        assert last_error is not None
        raise last_error

    async def _login(
        self, client: httpx.AsyncClient, username: str, password: str, ocr_service_url: str
    ) -> dict:
        """Full OAuth2/PKCE login on ``client`` (``follow_redirects=False``).

        Returns ``{"token","api"}``. Raises :class:`_MofidInvalidCredentials` on
        the wrong-password marker; retries on a captcha marker; ``RuntimeError``
        if every attempt fails. ``_finish_oauth`` then calls ``same-login`` (it is
        REQUIRED — the authed reads 403 without it).
        """
        verifier, challenge = _pkce()
        r = await client.get(
            _OAUTH_HOST + "/connect/authorize/callback",
            params={
                "client_id": _CLIENT_ID, "redirect_uri": _REDIRECT_URI,
                "response_type": "code", "scope": _SCOPE,
                "code_challenge": challenge, "code_challenge_method": "S256",
                "response_mode": "query",
            },
        )
        loc = r.headers.get("location")
        if not loc or "رد درخواست" in (r.text or ""):
            raise RuntimeError("mofid authorize rejected")
        login_url = urljoin(_OAUTH_HOST, loc)
        rp = await client.get(login_url)
        hops = 0
        while rp.status_code in (301, 302) and hops < 2:
            login_url = urljoin(login_url, rp.headers.get("location", ""))
            rp = await client.get(login_url)
            hops += 1

        last_desc: Optional[str] = None
        for _ in range(_MAX_CAPTCHA_ATTEMPTS):
            html = rp.text or ""
            body = {
                "Username": username, "Password": password,
                "__RequestVerificationToken": _form_field(html, "__RequestVerificationToken"),
                "button": "login", "RememberLogin": "false",
            }
            if 'name="Captcha"' in html or "OLoginCaptcha_CaptchaImage" in html:
                for f in _BDC_FIELDS:
                    body[f] = _form_field(html, f)
                m = _CAPTCHA_IMG_RE.search(html)
                if m:
                    img_url = urljoin(_OAUTH_HOST + "/", m.group(1).replace("&amp;", "&"))
                    try:
                        ir = await client.get(img_url)
                        cap_b64 = base64.b64encode(ir.content).decode()
                        body["Captcha"] = await self._solve_captcha(client, ocr_service_url, cap_b64)
                    except httpx.HTTPError as exc:
                        last_desc = f"captcha fetch/decode failed: {exc}"
                        rp = await client.get(login_url)
                        continue

            rl = await client.post(
                login_url, data=body,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            loc = rl.headers.get("location", "")
            if rl.status_code in (301, 302) and loc.startswith("/connect/authorize"):
                return await self._finish_oauth(client, loc, verifier)

            reject = _classify_mofid_login(rl.text)
            if reject == "invalid_credentials":
                raise _MofidInvalidCredentials("the broker rejected this username/password")
            last_desc = reject or f"login failed (status={rl.status_code})"
            rp = await client.get(login_url)  # fresh token + captcha

        raise RuntimeError(
            f"mofid login failed after {_MAX_CAPTCHA_ATTEMPTS} attempts"
            + (f": {last_desc}" if last_desc else "")
        )

    async def _finish_oauth(self, client: httpx.AsyncClient, authorize_loc: str, verifier: str) -> dict:
        rc = await client.get(urljoin(_OAUTH_HOST, authorize_loc))
        if "رد درخواست" in (rc.text or ""):
            raise RuntimeError("mofid authorize-continue rejected")
        m = _CODE_RE.search(rc.headers.get("location", ""))
        if not m:
            raise RuntimeError("mofid: no auth code")
        rt = await client.post(
            _OAUTH_HOST + "/connect/token",
            data={
                "client_id": _CLIENT_ID, "code": m.group(1), "redirect_uri": _REDIRECT_URI,
                "code_verifier": verifier, "grant_type": "authorization_code",
            },
            headers={"content-type": "application/x-www-form-urlencoded", "Referer": _REFERER},
        )
        try:
            tok = rt.json()
        except ValueError as exc:
            raise RuntimeError(f"mofid token non-JSON (status={rt.status_code})") from exc
        at = tok.get("access_token")
        if not at:
            raise RuntimeError(f"mofid token failed: {tok.get('error')}")
        # Device registration is REQUIRED for the authed reads — without it
        # /easy/api/account/user-info and /core/api/* return 403 "You do not have
        # permission to view this object" (live-confirmed). Best-effort; the bot
        # adapter calls it too, so it is not a verify-only side effect. (Mofid's
        # "same-login" permits concurrent same-account logins, registering this
        # session rather than evicting others.)
        try:
            await client.post(
                _API_HOST + "/easy/api/account/same-login",
                json={"uuid": "sm-mgmt", "appBuildNo": "16872", "width": 1536,
                      "height": 729, "devicePlatform": "Desktop", "platformInfo": _UA},
                headers={"Authorization": f"Bearer {at}", "User-Agent": _UA, "Referer": _REFERER},
            )
        except Exception:  # noqa: BLE001 — same-login best-effort
            logger.debug("mofid same-login failed (non-fatal)")
        return {"token": at, "api": _API_HOST}

    def _session_key(self, username: str, password: str) -> tuple[str, str, str]:
        return (self.code, username, _pw_fingerprint(password))

    def _invalidate_session(self, username: str, password: str) -> None:
        _SESSION_CACHE.pop(self._session_key(username, password), None)

    async def _session(self, username: str, password: str, ocr_service_url: str) -> dict:
        key = self._session_key(username, password)
        cached = _SESSION_CACHE.get(key)
        if cached is not None and time.monotonic() < cached["expires_at"]:
            return cached
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=_HTTP_TIMEOUT_S, trust_env=False,
            headers={"User-Agent": _UA, "Referer": _REFERER},
        ) as client:
            session = await self._login(client, username, password, ocr_service_url)
        session["expires_at"] = time.monotonic() + _SESSION_TTL_S
        _SESSION_CACHE[key] = session
        return session

    def _read_client(self, session: dict) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=True, timeout=_HTTP_TIMEOUT_S, trust_env=False,
            headers={
                "User-Agent": _UA, "Referer": _REFERER,
                "Authorization": f"Bearer {session['token']}",
                "Accept": "application/json, text/plain, */*",
            },
        )

    # -- BrokerAdapter contract -------------------------------------------
    async def verify_credentials(
        self, username: str, password: str, ocr_service_url: str
    ) -> VerifyResult:
        """Log in once; report success with the broker-confirmed account name."""
        try:
            session = await self._session(username, password, ocr_service_url)
            name = None
            bourse = None
            try:
                async with self._read_client(session) as client:
                    resp = await client.get(session["api"] + "/easy/api/account/user-info")
                if resp.status_code == 200:
                    info = resp.json() or {}
                    bourse = info.get("bourseCode") or None
                    name = (
                        f"{info.get('name', '')} {info.get('family', '')}".strip() or None
                    )
            except Exception:  # noqa: BLE001 — a valid login is success even if the
                pass           # bonus user-info read fails (separate host/perm).
            return VerifyResult(
                ok=True, status=CredStatus.VALID,
                full_name=name or bourse, national_id=None,
                bourse_code=bourse, message="login ok",
            )
        except _MofidInvalidCredentials as exc:
            return VerifyResult(
                ok=False, status=CredStatus.INVALID_CREDENTIALS,
                error="The broker rejected this username/password.",
                message=str(exc) or None,
            )
        except Exception as exc:  # noqa: BLE001 — surface anything else as transient
            return VerifyResult(ok=False, status=CredStatus.TRANSIENT, error=str(exc))

    async def verify_isin(
        self, username: str, password: str, isin: str, ocr_service_url: str
    ) -> IsinInfo:
        """Validate an ISIN against the public RLC backend (Mofid is Tadbir-based);
        no login/captcha needed. The creds args are unused (contract parity)."""
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
        """Fetch EXECUTED orders from Mofid's ``GET /core/api/order``.

        ⚠️ This endpoint returns RECENT orders (no date-range param confirmed in
        the spike), so ``from_date``/``to_date`` are advisory — surfaced as a
        warning, mirroring exir's truncation note. Keeps rows that traded
        (``executedQuantity > 0``); one re-login retry on a non-200.
        """
        if include_status is not None and set(include_status) != {3}:
            return [], (
                "mofid adapter supports filled orders only; "
                f"got include_status={include_status}"
            )
        if side is not None:
            return [], "mofid adapter does not support a side filter"
        try:
            last_err: Optional[str] = None
            for _attempt in range(2):
                session = await self._session(username, password, ocr_service_url)
                async with self._read_client(session) as client:
                    resp = await client.get(session["api"] + "/core/api/order")
                if resp.status_code != 200:
                    last_err = f"mofid GET /core/api/order HTTP {resp.status_code}: {resp.text[:200]}"
                    self._invalidate_session(username, password)
                    continue
                rows = (resp.json() or {}).get("orders") or []
                rows = [r for r in rows if int(r.get("executedQuantity") or 0) > 0]
                if isin:
                    rows = [r for r in rows if r.get("symbolIsin") == isin]
                return rows, (
                    "mofid /core/api/order returns recent orders only "
                    "(no date-range filter) — date range advisory"
                )
            return [], last_err
        except Exception as exc:  # noqa: BLE001
            logger.warning("mofid get_orders failed: %s", exc)
            return [], f"mofid error: {exc}"

    async def get_holdings(
        self, username: str, password: str, isin: str, *, ocr_service_url: str
    ) -> int:
        """Whole-share holding for ``isin`` via ``GET /core/api/portfolio/true``.
        Absent ISIN is a VALID answer (holds nothing) → 0. Raises on a non-200."""
        session = await self._session(username, password, ocr_service_url)
        async with self._read_client(session) as client:
            resp = await client.get(session["api"] + "/core/api/portfolio/true")
        if resp.status_code != 200:
            self._invalidate_session(username, password)
            raise httpx.HTTPError(f"mofid portfolio HTTP {resp.status_code}")
        for row in (resp.json() or {}).get("portfolioItems") or []:
            if isinstance(row, dict) and row.get("isin") == isin:
                return int(row.get("asset") or 0)
        return 0
