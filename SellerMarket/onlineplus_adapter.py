"""OnlinePlus (Tadbir "Online+") broker adapter — reference tenant Hafez (Phase 2).

A THIRD broker family alongside ephoenix (static Bearer) and exir (cookie +
per-request X-App-N signature). OnlinePlus is structurally **"exir minus the
signer"**: a plain COOKIE session — a successful login on the per-tenant
``api.{code}broker.ir`` host sets an ``AuthCookie_OnlineCookie`` (+ F5 cookies)
that authorizes every subsequent call. No Bearer header, no per-request
signature. Its 4-digit captcha is solved by the dedicated OCR CNN route
``/ocr/onlineplusplatforms-base64`` (NOT the 5-digit easy route). Market data
(the daily price band) is the SAME public RLC backend exir uses
(:mod:`rlc_price`) — so BUY fires at the ceiling, SELL at the floor, identically.

Confirmed live (read-only spike against Hafez, account 4580090306 — no orders):
* web host ``online.{code}broker.ir`` embeds ``var ApiBaseURl = '...'`` →
  ``api.{code}broker.ir``.
* ``GET {api}/Web/V1/Authenticate/GetCaptchaImage/Captcha`` →
  ``{Data:{Captcha:<b64 PNG>, CaptchaKey}}``.
* ``POST {api}/Web/V2/Authenticate/Login`` ``{UserName,Password,Captcha,
  CaptchaKey}`` → ``{IsSuccessfull, Data:{Token, CustomerName, ActiveSms,
  ActiveOtp, MustChangePassword, ...}}`` + Set-Cookie. Auth carrier = COOKIES.
* reject markers (HTTP 200, ``IsSuccessfull:false``, on ``MessageCode``):
  ``oms_1000`` wrong password, ``InvalidCaptcha`` wrong captcha.
* ``GET {api}/Web/V1/Accounting/Remain`` → ``Data.PurchasingPower`` (buying power).
* ``GET {api}/Web/V1/RealtimePortfolio/...`` → holdings (``RemainQuantity``).

Order PLACEMENT (the body's ``orderSide``/``orderValidity`` encodings + the
Caution/Sepah flags) comes from the decompiled desktop client and is NOT yet
live-fired — confirm in the canary before fleet rollout. No buy-fee endpoint
exists, so BUY sizing uses a conservative fallback fee (over-estimate, never
over-spend), like exir's wages-miss fallback.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Optional

import requests

import rlc_price
import runtime_config
from broker_adapters import BrokerAdapter, PreparedOrder, SellContext
from captcha_utils import decode_captcha as _default_decode_captcha
from cred_errors import InvalidCredentialsError, onlineplus_login_is_invalid_credentials
from exir_token import pw_fingerprint

logger = logging.getLogger(__name__)

CAPTCHA_RETRIES = 6
TIMEOUT = 20
# OnlinePlus login returns no validity-minutes field, so use a conservative fixed
# session lifetime and re-login on expiry. (The order burst is seconds; reads in
# prepare_order are off the hot path.)
_SESSION_TTL_S = 600.0
# No buy-fee endpoint exists on OnlinePlus, so BUY volume is sized with a
# conservative fallback fee — deliberately ABOVE a typical real rate so a missing
# fee under-sizes the order slightly instead of over-spending the buying power
# (an over-spend is a guaranteed broker rejection). Same rationale as exir.
ONLINEPLUS_FALLBACK_BUY_FEE = 0.005

# The 4-digit OnlinePlus captcha needs the dedicated CNN OCR route.
_OCR_PATH = "/ocr/onlineplusplatforms-base64"

# Order payload enum values (decompiled CheetahPlus OnlinePlusWebApi — NOT yet
# live-fired; confirm in the canary). orderSide: 65 = Buy, 86 = Sell.
# orderValidity: 74 = Day.
_ORDER_SIDE_BUY = 65
_ORDER_SIDE_SELL = 86
_ORDER_VALIDITY_DAY = 74

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Scrape the API base URL out of the web login page (``var ApiBaseURl = '...'``).
_API_BASE_RE = re.compile(r"ApiBaseURl\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)

# Module-level caches, cleared on process restart (re-login / re-scrape cost ==
# cold-start cost; acceptable). Session entry:
# {"api","cookies","customer_name","expires_at"}.
_SESSION_CACHE: dict = {}
_API_BASE_CACHE: dict = {}


def _order_body(isin: str, side: int, price: float, volume: int) -> str:
    """Build the OnlinePlus ``/Web/V1/Order/Post`` JSON body (string).

    Field set + the orderSide/orderValidity enums are from the decompiled
    desktop client — confirm against a real fill in the canary (G7). Price and
    quantity are strings (the platform expects string-typed numbers).
    """
    return json.dumps({
        "isin": isin,
        "orderCount": str(int(volume)),
        "orderPrice": str(int(price)),
        "orderSide": _ORDER_SIDE_BUY if int(side) == 1 else _ORDER_SIDE_SELL,
        "orderValidity": _ORDER_VALIDITY_DAY,
        "CautionAgreementSelected": False,
        "FinancialProviderId": 1,
        "IsSymbolCautionAgreement": False,
        "IsSymbolSepahAgreement": False,
        "SepahAgreementSelected": False,
        "maxShow": 0,
        "minimumQuantity": 0,
        "orderId": 0,
        "orderValiditydate": None,
        "shortSellIncentivePercent": 0,
        "shortSellIsEnabled": False,
    })


class OnlinePlusAdapter(BrokerAdapter):
    """Adapter for the OnlinePlus / Tadbir Online+ broker family (Hafez et al.)."""

    family = "onlineplus"

    def __init__(
        self,
        broker_code: str,
        username: str,
        password: str,
        captcha_decoder: Optional[Callable[..., str]] = None,
        cache: Optional[Any] = None,
        config_section: Optional[dict] = None,
    ):
        self.broker_code = broker_code
        self.username = username
        self.password = password
        self.captcha_decoder = captcha_decoder or _default_decode_captcha
        self.cache = cache  # unused (no shared cache schema); kept for signature parity
        # OnlinePlus tenants don't share one host convention (Hafez =
        # hafezbroker.ir, but dnovin = dnovinbr.ir), so the mgmt UI renders the
        # per-broker domain into config.ini as ``onlineplus_base_domain``.
        # Resolve order: the rendered base_domain -> the [runtime]
        # ``onlineplus_web_<code>`` override -> the legacy ``{code}broker.ir``
        # convention. The API base is scraped off the web host (see _api_base);
        # ``_api_convention`` is the fallback when the scrape fails.
        domain = str((config_section or {}).get("onlineplus_base_domain") or "").strip()
        if domain:
            self._web_base = f"https://online.{domain}"
            self._api_convention = f"https://api.{domain}"
        else:
            self._web_base = runtime_config.get(
                f"onlineplus_web_{broker_code}", f"https://online.{broker_code}broker.ir"
            ).rstrip("/")
            self._api_convention = f"https://api.{broker_code}broker.ir"

    @staticmethod
    def _ocr_path() -> str:
        return runtime_config.get("onlineplus_ocr_path", _OCR_PATH)

    # ---- host discovery ---------------------------------------------------

    def _api_base(self, session: requests.Session) -> str:
        """Return the API base URL (e.g. ``https://api.hafezbroker.ir``).

        Resolve order: ``[runtime] onlineplus_api_<code>`` override → module
        cache → scrape ``var ApiBaseURl`` from the web login page → convention
        ``api.{code}broker.ir``. Cached module-level so multiple accounts on one
        stack don't each scrape.
        """
        cached = _API_BASE_CACHE.get(self.broker_code)
        if cached:
            return cached
        override = runtime_config.get(f"onlineplus_api_{self.broker_code}", "")
        if override:
            api = override.rstrip("/")
        else:
            api = self._api_convention  # base_domain-derived or {code}broker.ir
            try:
                resp = session.get(f"{self._web_base}/Account/Login", timeout=TIMEOUT)
                m = _API_BASE_RE.search(resp.text or "")
                if m:
                    api = m.group(1).strip().rstrip("/")
            except Exception as exc:  # noqa: BLE001 — fall back to the convention
                logger.warning(
                    "onlineplus %s: could not scrape ApiBaseURl (%s); using %s",
                    self.broker_code, exc, api,
                )
        _API_BASE_CACHE[self.broker_code] = api
        return api

    # ---- auth / session ---------------------------------------------------

    def _cache_key(self) -> tuple:
        return (self.broker_code, self.username, pw_fingerprint(self.password))

    def _login(self, session: requests.Session) -> dict:
        """Log in and return a session descriptor.

        Returns ``{"api","cookies","customer_name","expires_at"}``. Raises
        :class:`InvalidCredentialsError` on the ``oms_1000`` marker (skip the
        account), ``RuntimeError`` on an OTP/password-change-required account
        (creds valid but not auto-tradable) or if captcha/login never succeeds.
        Retries on ``InvalidCaptcha``.
        """
        api = self._api_base(session)
        last_desc: Optional[str] = None
        for attempt in range(1, CAPTCHA_RETRIES + 1):
            rc = session.get(
                f"{api}/Web/V1/Authenticate/GetCaptchaImage/Captcha", timeout=TIMEOUT
            )
            try:
                cdata = (rc.json() or {}).get("Data") or {}
                b64 = cdata["Captcha"]
                captcha_key = cdata["CaptchaKey"]
            except Exception:  # noqa: BLE001 — malformed captcha; refetch
                last_desc = f"malformed captcha response (HTTP {rc.status_code})"
                continue

            cap = self.captcha_decoder(b64, ocr_path=self._ocr_path())
            logger.debug(
                "onlineplus login attempt %s: ocr_len=%s",
                attempt, len(cap) if cap else 0,
            )
            # The OnlinePlus captcha is exactly 4 numeric digits.
            if not (cap and cap.isdigit() and len(cap) == 4):
                last_desc = f"OCR returned {cap!r} (expected 4 digits)"
                continue

            rl = session.post(
                f"{api}/Web/V2/Authenticate/Login",
                json={
                    "UserName": self.username,
                    "Password": self.password,
                    "Captcha": cap,
                    "CaptchaKey": captcha_key,
                },
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=TIMEOUT,
            )
            try:
                body = rl.json()
            except Exception:  # noqa: BLE001
                last_desc = f"login non-JSON (HTTP {rl.status_code})"
                continue

            data = body.get("Data") or {}
            if body.get("IsSuccessfull") and data.get("Token"):
                if data.get("ActiveOtp") or data.get("ActiveSms"):
                    raise RuntimeError(
                        f"OnlinePlus account {self.username}@{self.broker_code} has "
                        "SMS/OTP enabled — not auto-tradable (disable OTP to trade)"
                    )
                if data.get("MustChangePassword"):
                    raise RuntimeError(
                        f"OnlinePlus account {self.username}@{self.broker_code} must "
                        "change its password — not auto-tradable until changed"
                    )
                logger.info(
                    "onlineplus login ok for %s@%s", self.username, self.broker_code
                )
                return {
                    "api": api,
                    "cookies": dict(session.cookies),
                    "customer_name": data.get("CustomerName"),
                    "expires_at": time.monotonic() + _SESSION_TTL_S,
                }

            # High-confidence wrong-password reject → skip the account.
            if onlineplus_login_is_invalid_credentials(body):
                raise InvalidCredentialsError(
                    f"onlineplus rejected credentials for "
                    f"{self.username}@{self.broker_code}"
                )
            # Wrong captcha (or any other ambiguous failure) → retry.
            last_desc = (
                body.get("MessageDesc")
                or body.get("MessageCode")
                or f"login failed (HTTP {rl.status_code})"
            )

        raise RuntimeError(
            f"OnlinePlus login failed for {self.username}@{self.broker_code} "
            f"after {CAPTCHA_RETRIES} captcha attempts"
            + (f": {last_desc}" if last_desc else "")
        )

    def _session(self) -> dict:
        """Return a valid session descriptor, logging in (and caching) if needed."""
        key = self._cache_key()
        entry = _SESSION_CACHE.get(key)
        if entry and entry.get("expires_at", 0) > time.monotonic():
            return entry
        session = requests.Session()
        session.trust_env = False  # reach the Iranian host directly, never via a proxy
        session.headers.update({
            "User-Agent": UA,
            "Accept": "*/*",
            "Origin": self._web_base,
            "Referer": self._web_base + "/",
        })
        descriptor = self._login(session)
        _SESSION_CACHE[key] = descriptor
        return descriptor

    # ---- cookie reads -----------------------------------------------------

    def _get(self, path: str, descriptor: dict) -> Any:
        """Cookie-authed ``GET base+path`` → parsed JSON. Raises on HTTP error."""
        resp = requests.get(
            descriptor["api"] + path,
            headers={"Accept": "application/json", "User-Agent": UA},
            cookies=descriptor["cookies"],
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _buying_power(self, descriptor: dict) -> float:
        """Spendable cash via ``GET /Web/V1/Accounting/Remain`` → ``PurchasingPower``."""
        data = self._get("/Web/V1/Accounting/Remain", descriptor)
        d = (data or {}).get("Data") or {}
        return float(d.get("PurchasingPower") or d.get("AccountBalance") or 0)

    def _holdings(self, isin: str, descriptor: dict) -> int:
        """Whole-share holding for ``isin`` via ``RealtimePortfolio``.

        Probes both ``SymbolISIN`` (RealtimePortfolio) and ``SymbolIsin``
        (GetOrderList) casings, which the platform mixes."""
        path = (
            "/Web/V1/RealtimePortfolio/Get/RealtimePortfolio"
            "?GetJustHasRemain=true&EndDate=undefined&BasedOnLastPositivePeriod=true"
            "&ActiveSymbolsStartDate=&ActiveSymbolsEndDate="
        )
        data = self._get(path, descriptor)
        rows = (data or {}).get("Data") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = row.get("SymbolISIN") or row.get("SymbolIsin")
            if code == isin:
                return int(row.get("RemainQuantity") or 0)
        return 0

    # ---- order prep -------------------------------------------------------

    def prepare_order(self, *, isin: str, side: int, config_section: dict) -> PreparedOrder:
        """Prepare one OnlinePlus order; may raise on any auth/sizing/config failure."""
        side = int(side)
        config_section = config_section or {}

        # Daily allowed price band from the broker's own RLC gateway (same source
        # as exir): BUY at the ceiling (limit-up), SELL at the floor (limit-down).
        # A config `price` is honoured only as an explicit override.
        override = config_section.get("price")
        if override:
            ceiling = floor = float(override)
        else:
            ceiling, floor = rlc_price.get_price_band(isin)
        price = float(ceiling if side == 1 else floor)
        if price <= 0:
            raise ValueError(f"no rlc price band for {isin}")

        try:
            descriptor = self._session()

            if side == 1:  # BUY — size from spendable cash, NET OF a fallback fee.
                bp = self._buying_power(descriptor)
                fee = runtime_config.get_float(
                    "onlineplus_fallback_buy_fee", ONLINEPLUS_FALLBACK_BUY_FEE
                )
                # floor(BP / (price * (1 + fee))) — fee-adjusted so the order can't
                # over-spend the buying power (which the broker would reject).
                volume = int(bp / (price * (1.0 + fee)))
                logger.info(
                    "onlineplus BUY %s (%s@%s): bp=%s, price=%s, fee=%s, raw_volume=%s",
                    isin, self.username, self.broker_code, f"{bp:,.0f}", price, fee,
                    f"{volume:,}",
                )
            else:  # SELL — size from real holdings; fail-fast on nothing held.
                volume = self._holdings(isin, descriptor)
                if volume <= 0:
                    raise ValueError(f"no OnlinePlus holdings for {isin}")
                logger.info(
                    "onlineplus SELL %s (%s@%s): holdings/raw_volume=%s",
                    isin, self.username, self.broker_code, f"{volume:,}",
                )

            # Cap at the instrument's MAX ORDER QUANTITY (RLC mxqo), then the
            # operator-set hard cap.
            max_qty = rlc_price.get_max_order_qty(isin)
            if max_qty and 0 < max_qty < volume:
                logger.warning(
                    "onlineplus %s (%s@%s): volume %s exceeds max order qty %s — capping",
                    isin, self.username, self.broker_code, f"{volume:,}", f"{max_qty:,}",
                )
                volume = max_qty
            max_volume = config_section.get("max_volume")
            if max_volume:
                volume = min(volume, int(max_volume))
            if volume <= 0:
                raise ValueError(
                    f"onlineplus {isin}: computed order volume is 0 (bp/holdings too small)"
                )
            logger.info(
                "onlineplus %s (%s@%s): final volume=%s (max_order_qty=%s)",
                isin, self.username, self.broker_code, f"{volume:,}", max_qty or "n/a",
            )

            order_path = runtime_config.get("onlineplus_order_path", "/Web/V1/Order/Post")
            return PreparedOrder(
                order_url=descriptor["api"] + order_path,
                body=_order_body(isin, side, price, volume),
                bearer_token=None,
                signer=None,
                cookies=descriptor["cookies"],
                price=price,
                volume=volume,
            )
        except (ValueError, InvalidCredentialsError):
            # Domain errors + the credential reject propagate AS-IS (the generic
            # wrap below would hide InvalidCredentialsError from the caller's skip
            # branch — the exact bug exir fixed).
            raise
        except Exception as e:
            raise RuntimeError(
                f"OnlinePlus prepare_order failed for {self.username}@{self.broker_code} "
                f"isin={isin} side={side}: {e}"
            ) from e

    def open_sell_context(self, *, isin: str, config_section: dict) -> SellContext:
        """Auto-sell context (#110): floor = RLC band ``lap``, cap = ``mxqo``.

        Cookie-auth SELL (no signer). ``fetch_holdings`` re-reads LIVE via
        ``RealtimePortfolio``; ``prepare_chunk(volume)`` builds the byte-identical
        ``/Web/V1/Order/Post`` SELL body at the floor.
        """
        config_section = config_section or {}
        override = config_section.get("price")
        if override:
            floor = int(float(override))
        else:
            _ceiling, floor = rlc_price.get_price_band(isin)
            floor = int(floor)
        if floor <= 0:
            raise ValueError(f"no rlc price band (floor) for {isin}")
        cap = int(rlc_price.get_max_order_qty(isin) or 0)
        # Validate auth up-front (fail-fast on bad creds / OTP) so open_sell_context
        # raises now rather than only when the monitor first triggers a sell.
        self._session()
        order_path = runtime_config.get("onlineplus_order_path", "/Web/V1/Order/Post")

        def fetch_holdings() -> int:
            d = self._session()  # refreshes on expiry
            return int(self._holdings(isin, d) or 0)

        def prepare_chunk(volume: int) -> PreparedOrder:
            d = self._session()
            return PreparedOrder(
                order_url=d["api"] + order_path,
                body=_order_body(isin, 2, floor, int(volume)),
                bearer_token=None,
                signer=None,
                cookies=d["cookies"],
                price=floor,
                volume=int(volume),
            )

        return SellContext(
            floor_price=floor,
            max_order_volume=cap,
            fetch_holdings=fetch_holdings,
            prepare_chunk=prepare_chunk,
        )
