"""Mofid / Orbis (easytrader.ir) broker adapter — the 4th broker family.

A FOURTH family alongside ephoenix (static Bearer), exir (cookie + X-App-N), and
onlineplus (cookie). Mofid carries a Bearer token like ephoenix, but obtains it
via a full **OAuth2 Authorization-Code + PKCE** login against ``login.emofid.com``
(an HTML-form scrape with an OPTIONAL BotDetect captcha that appears on retry),
then drives the ``api-mts.orbis.easytrader.ir`` JSON API with ``Authorization:
Bearer``.

Order firing is the SPA's **draft + batch** flow (NOT a single POST): pre-create
N server-side drafts (``POST /easy/api/draft`` → ``{id}``) then fire the batch
(``POST /core/api/order/batchCreate`` ``{draftIds, removeDraftAfterCreate,
orderFrom}``). Because firing is bounded by Mofid's **1500-requests/hour** cap,
the BUY path runs via the dedicated :mod:`mofid_firer` (NOT the locust spam) —
see ``run_mofid.py``. Auto-sell uses the single immediate order
(``POST /core/api/v2/order``).

Market data (the daily price band) reuses the shared public RLC backend
(:mod:`rlc_price`); no Mofid wages endpoint exists, so BUY sizing uses a
conservative fallback fee (over-estimate, never over-spend).

LIVE-CONFIRMED by a read-only Phase-0 spike (no orders) — see
``scratch/MOFID_FINDINGS.md``. FLAT package layout (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Callable, Optional
from urllib.parse import urljoin

import requests

import rlc_price
import runtime_config
from broker_adapters import BrokerAdapter, PreparedOrder, SellContext
from captcha_utils import decode_captcha as _default_decode_captcha
from cred_errors import InvalidCredentialsError, mofid_login_reject
from exir_token import pw_fingerprint

logger = logging.getLogger(__name__)

# --- hosts / OAuth constants (LIVE-confirmed) ------------------------------
API_HOST = "https://api-mts.orbis.easytrader.ir"
OAUTH_HOST = "https://login.emofid.com"
REDIRECT_URI = "https://d.easytrader.ir/auth-callback"
REFERER = "https://d.easytrader.ir/"
CLIENT_ID = "easy_pkce"
SCOPE = "easy2_api mts_api openid profile"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CAPTCHA_RETRIES = 6
TIMEOUT = 20
# The BotDetect captcha on login.emofid.com uses the dedicated mofid-orbis route.
_OCR_PATH = "/ocr/mofid-orbis-base64"
# No Mofid wages endpoint exists → conservative fallback buy fee (over-estimate,
# never over-spend), same rationale as exir/onlineplus.
MOFID_FALLBACK_BUY_FEE = 0.005
# Drafts per fire. Each draft is the FULL-volume order, so a batch of N can fill
# at most ONE (the rest reject for insufficient buying power → NO over-buy). 1 =
# safest (canary default); Orbis.py used 10 for queue-race redundancy. Config:
# [runtime] mofid_draft_count.
DRAFT_COUNT_DEFAULT = 1
# Floors so a tiny / missing expires_in can't thrash logins.
_MIN_TTL_SECONDS = 60.0
_REFRESH_MARGIN_S = 120.0
# PKCE verifier byte length → 96 url-safe base64 chars (matches decompiled Pkce(96)).
_PKCE_BYTES = 72

# Module session cache keyed (code, user, pw_fingerprint) → descriptor
# {"token", "expires_at" (epoch)}.
_SESSION_CACHE: dict = {}

# Dedicated read/draft/batch session: trust_env=False so the Bearer calls reach
# the Iranian host DIRECTLY (never a foreign proxy). The Bearer is passed per-call,
# so the session carries no cross-account state.
_READ_SESSION = requests.Session()
_READ_SESSION.trust_env = False

_VALIDATION_RE = re.compile(r"validation-summary-errors", re.I)
_CAPTCHA_IMG_RE = re.compile(r'id="OLoginCaptcha_CaptchaImage"[^>]*src="([^"]+)"')
_CODE_RE = re.compile(r"[?&]code=([^&]+)")
_BDC_FIELDS = (
    "BDC_VCID_OLoginCaptcha",
    "BDC_BackWorkaround_OLoginCaptcha",
    "BDC_Hs_OLoginCaptcha",
    "BDC_SP_OLoginCaptcha",
)


def _mofid_side(config_side: int) -> int:
    """Bot config side (1=buy / 2=sell) → Mofid wire side (Buy=0 / Sell=1)."""
    return 0 if int(config_side) == 1 else 1


def _field(html: str, name: str) -> str:
    m = re.search(r'name="' + re.escape(name) + r'"[^>]*value="([^"]*)"', html or "")
    return m.group(1) if m else ""


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(_PKCE_BYTES)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def mofid_response_ok(status: int, body: bytes) -> bool:
    """True iff a Mofid order/batch response indicates a genuinely placed order.

    HTTP 200 alone is NOT enough — ``batchCreate`` / ``v2/order`` can return 200
    with a failure envelope. Success ⇔ 200 AND the body is not an obvious error
    (not ``isSuccessful:false``, no top-level ``error``, no ``omsError`` with the
    market-closed code ``8706``). Refined at the canary against a real fill so a
    soft failure is never logged as fired."""
    if status != 200:
        return False
    try:
        data = json.loads((body or b"").decode("utf-8", "replace") or "null")
    except Exception:  # noqa: BLE001 — non-JSON 200 → not confirmed
        return False
    if data is None:
        return True  # empty 200 body
    if isinstance(data, dict):
        if data.get("isSuccessful") is False:
            return False
        if data.get("error"):
            return False
        oms = data.get("omsError")
        if isinstance(oms, list) and any(
            isinstance(e, dict) and e.get("code") == 8706 for e in oms
        ):
            return False
    return True


class MofidAdapter(BrokerAdapter):
    """Adapter for the Mofid / Orbis broker family (single broker, no tenant)."""

    family = "mofid"

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
        self.cache = cache  # unused (signature parity)
        self.config_section = config_section or {}

    # ---- runtime-config endpoints (DB-pushable, no rebuild) ---------------
    @staticmethod
    def _ocr_path() -> str:
        return runtime_config.get("mofid_ocr_path", _OCR_PATH)

    def _api_host(self) -> str:
        return runtime_config.get("mofid_api_host", API_HOST).rstrip("/")

    def _draft_url(self) -> str:
        return runtime_config.get("mofid_draft_url", f"{self._api_host()}/easy/api/draft")

    def _batch_url(self) -> str:
        return runtime_config.get(
            "mofid_batch_url", f"{self._api_host()}/core/api/order/batchCreate"
        )

    def _order_url(self) -> str:  # single immediate order (auto-sell SELL)
        return runtime_config.get("mofid_order_url", f"{self._api_host()}/core/api/v2/order")

    # ---- auth / session ---------------------------------------------------
    def _cache_key(self) -> tuple:
        return (self.broker_code, self.username, pw_fingerprint(self.password))

    def _token_file(self) -> str:
        d = runtime_config.get(
            "mofid_token_dir", os.path.join(os.path.dirname(__file__), "run_results")
        )
        return os.path.join(d, f"mofid_token_{self.broker_code}_{self.username}.json")

    def _read_token_file(self) -> Optional[dict]:
        try:
            with open(self._token_file(), "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("access_token") and float(d.get("expires_at", 0)) > time.time() + _REFRESH_MARGIN_S:
                return {"token": d["access_token"], "expires_at": float(d["expires_at"])}
        except (FileNotFoundError, ValueError, OSError, KeyError, TypeError):
            return None
        return None

    def _write_token_file(self, token: str, expires_at: float) -> None:
        path = self._token_file()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"access_token": token, "expires_at": expires_at}, f)
            os.replace(tmp, path)  # atomic on the same fs; safe in a DIR mount
        except OSError as exc:  # noqa: BLE001 — token persistence is best-effort
            logger.debug("mofid: could not persist token file (%s)", exc)

    def _login(self) -> dict:
        """Full OAuth2/PKCE login → ``{"token","expires_at"}``.

        Raises :class:`InvalidCredentialsError` on the wrong-password marker
        (skip the account) or ``RuntimeError`` if captcha/login never succeeds.
        Manual redirects (``allow_redirects=False``); the session carries cookies.
        """
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": UA})
        verifier, challenge = _pkce()

        # Step 1 — authorize → 302 /Login?ReturnUrl=
        r = s.get(
            OAUTH_HOST + "/connect/authorize/callback",
            params={
                "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
                "response_type": "code", "scope": SCOPE,
                "code_challenge": challenge, "code_challenge_method": "S256",
                "response_mode": "query",
            },
            allow_redirects=False, timeout=TIMEOUT,
        )
        loc = r.headers.get("Location")
        if not loc or "رد درخواست" in (r.text or ""):
            raise RuntimeError(f"mofid authorize rejected for {self.username}@{self.broker_code}")
        login_url = urljoin(OAUTH_HOST, loc)

        # Step 2 — login page (follow one http→https hop if needed)
        rp = s.get(login_url, allow_redirects=False, timeout=TIMEOUT)
        hops = 0
        while rp.status_code in (301, 302) and hops < 2:
            login_url = urljoin(login_url, rp.headers.get("Location", ""))
            rp = s.get(login_url, allow_redirects=False, timeout=TIMEOUT)
            hops += 1

        last_desc: Optional[str] = None
        for attempt in range(1, CAPTCHA_RETRIES + 1):
            html = rp.text or ""
            token = _field(html, "__RequestVerificationToken")
            body = {
                "Username": self.username, "Password": self.password,
                "__RequestVerificationToken": token,
                "button": "login", "RememberLogin": "false",
            }
            has_captcha = 'name="Captcha"' in html or "OLoginCaptcha_CaptchaImage" in html
            if has_captcha:
                for f in _BDC_FIELDS:
                    body[f] = _field(html, f)
                m = _CAPTCHA_IMG_RE.search(html)
                if m:
                    img_url = urljoin(OAUTH_HOST + "/", m.group(1).replace("&amp;", "&"))
                    try:
                        ir = s.get(img_url, timeout=TIMEOUT)
                        cap_b64 = base64.b64encode(ir.content).decode()
                        body["Captcha"] = self.captcha_decoder(cap_b64, ocr_path=self._ocr_path())
                    except Exception as exc:  # noqa: BLE001 — captcha fetch/decode; retry
                        last_desc = f"captcha fetch/decode failed ({exc})"
                        rp = s.get(login_url, allow_redirects=False, timeout=TIMEOUT)
                        continue

            rl = s.post(login_url, data=body, allow_redirects=False, timeout=TIMEOUT)
            loc = rl.headers.get("Location", "")
            if rl.status_code in (301, 302) and loc.startswith("/connect/authorize"):
                return self._finish_oauth(s, loc, verifier)

            # Reject classification (HTML validation summary).
            reject = mofid_login_reject(rl.text)
            if reject == "invalid_credentials":
                raise InvalidCredentialsError(
                    f"mofid rejected credentials for {self.username}@{self.broker_code}"
                )
            last_desc = reject or f"login failed (HTTP {rl.status_code})"
            rp = s.get(login_url, allow_redirects=False, timeout=TIMEOUT)  # fresh token + captcha

        raise RuntimeError(
            f"mofid login failed for {self.username}@{self.broker_code} after "
            f"{CAPTCHA_RETRIES} attempts" + (f": {last_desc}" if last_desc else "")
        )

    def _finish_oauth(self, s: requests.Session, authorize_loc: str, verifier: str) -> dict:
        """Steps 6–8: follow the authorize redirect → code → token → same-login."""
        rc = s.get(urljoin(OAUTH_HOST, authorize_loc), allow_redirects=False, timeout=TIMEOUT)
        if "رد درخواست" in (rc.text or ""):
            raise RuntimeError(f"mofid authorize-continue rejected for {self.username}@{self.broker_code}")
        m = _CODE_RE.search(rc.headers.get("Location", ""))
        if not m:
            raise RuntimeError(f"mofid: no auth code for {self.username}@{self.broker_code}")
        rt = s.post(
            OAUTH_HOST + "/connect/token",
            data={
                "client_id": CLIENT_ID, "code": m.group(1), "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier, "grant_type": "authorization_code",
            },
            headers={"content-type": "application/x-www-form-urlencoded", "Referer": REFERER},
            allow_redirects=False, timeout=TIMEOUT,
        )
        try:
            tok = rt.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"mofid token non-JSON (HTTP {rt.status_code}): {exc}") from exc
        at = tok.get("access_token")
        if not at:
            raise RuntimeError(
                f"mofid token failed for {self.username}@{self.broker_code}: {tok.get('error')}"
            )
        ttl = max(_MIN_TTL_SECONDS, float(tok.get("expires_in") or 0))
        expires_at = time.time() + ttl
        # Device registration (best-effort; reads work without it). NOTE: mgmt
        # verify deliberately does NOT call same-login (it may evict the bot's
        # live session); the bot's trading login does, mirroring the SPA.
        try:
            s.post(
                f"{self._api_host()}/easy/api/account/same-login",
                json={
                    "uuid": "sm-bot", "appBuildNo": "16872", "width": 1536,
                    "height": 729, "devicePlatform": "Desktop", "platformInfo": UA,
                },
                headers={"Authorization": f"Bearer {at}", "User-Agent": UA, "Referer": REFERER},
                timeout=TIMEOUT,
            )
        except Exception:  # noqa: BLE001 — same-login best-effort
            logger.debug("mofid same-login failed (non-fatal) for %s@%s", self.username, self.broker_code)
        logger.info("mofid login ok for %s@%s", self.username, self.broker_code)
        return {"token": at, "expires_at": expires_at}

    def _session(self, force: bool = False) -> dict:
        """Return a valid session descriptor; reuse the cache / token file or log in."""
        key = self._cache_key()
        if not force:
            entry = _SESSION_CACHE.get(key)
            if entry and entry.get("expires_at", 0) > time.time() + _REFRESH_MARGIN_S:
                return entry
            filed = self._read_token_file()
            if filed:
                _SESSION_CACHE[key] = filed
                return filed
        descriptor = self._login()
        _SESSION_CACHE[key] = descriptor
        self._write_token_file(descriptor["token"], descriptor["expires_at"])
        return descriptor

    def _invalidate(self) -> None:
        _SESSION_CACHE.pop(self._cache_key(), None)

    # ---- Bearer reads -----------------------------------------------------
    def _authed_get(self, path_or_url: str, descriptor: dict) -> Any:
        url = path_or_url if path_or_url.startswith("http") else self._api_host() + path_or_url
        resp = _READ_SESSION.get(
            url,
            headers={
                "Authorization": f"Bearer {descriptor['token']}",
                "Accept": "application/json, text/plain, */*",
                "User-Agent": UA, "Referer": REFERER,
            },
            timeout=TIMEOUT,
        )
        if resp.status_code == 401:  # stale token → one re-login + retry
            self._invalidate()
            descriptor = self._session(force=True)
            resp = _READ_SESSION.get(
                url,
                headers={
                    "Authorization": f"Bearer {descriptor['token']}",
                    "Accept": "application/json, text/plain, */*",
                    "User-Agent": UA, "Referer": REFERER,
                },
                timeout=TIMEOUT,
            )
        resp.raise_for_status()
        return resp.json()

    def _buying_power(self, descriptor: dict) -> float:
        data = self._authed_get("/core/api/money/", descriptor)
        return float((data or {}).get("buyPower") or 0)

    def _holdings(self, isin: str, descriptor: dict) -> int:
        data = self._authed_get("/core/api/portfolio/true", descriptor)
        rows = (data or {}).get("portfolioItems") or []
        for row in rows:
            if isinstance(row, dict) and row.get("isin") == isin:
                return int(row.get("asset") or 0)
        return 0

    def server_time_offset_ms(self) -> int:
        """Broker-vs-local clock offset (ms) for the firing window (Orbis.py math)."""
        descriptor = self._session()
        local_ms = int(time.time() * 1000)
        data = self._authed_get(f"/easy/api/account/server-time/{local_ms}", descriptor)
        return int((data or {}).get("diff") or 0)

    # ---- sizing -----------------------------------------------------------
    def _size(self, isin: str, side: int, descriptor: dict) -> tuple[float, int]:
        config_section = self.config_section
        override = config_section.get("price")
        if override:
            ceiling = floor = float(override)
        else:
            ceiling, floor = rlc_price.get_price_band(isin)
        price = float(ceiling if side == 1 else floor)
        if price <= 0:
            raise ValueError(f"no rlc price band for {isin}")

        if side == 1:  # BUY — fee-adjusted from buying power
            bp = self._buying_power(descriptor)
            fee = runtime_config.get_float("mofid_fallback_buy_fee", MOFID_FALLBACK_BUY_FEE)
            volume = int(bp / (price * (1.0 + fee)))
            logger.info(
                "mofid BUY %s (%s@%s): bp=%s price=%s fee=%s raw_vol=%s",
                isin, self.username, self.broker_code, f"{bp:,.0f}", price, fee, f"{volume:,}",
            )
        else:  # SELL — from real holdings
            volume = self._holdings(isin, descriptor)
            if volume <= 0:
                raise ValueError(f"no mofid holdings for {isin}")
        max_qty = rlc_price.get_max_order_qty(isin)
        if max_qty and 0 < max_qty < volume:
            volume = int(max_qty)
        max_volume = config_section.get("max_volume")
        if max_volume:
            volume = min(volume, int(max_volume))
        if volume <= 0:
            raise ValueError(f"mofid {isin}: computed order volume is 0")
        return price, volume

    # ---- order prep -------------------------------------------------------
    def _draft_body(self, isin: str, side: int, price: float, volume: int) -> dict:
        return {
            "draft": {
                "symbolIsin": isin,
                "symbolName": self.config_section.get("symbolname") or isin,
                "price": int(price),
                "quantity": int(volume),
                "side": _mofid_side(side),
                "validityType": 0,
                "validityDate": None,
            }
        }

    def _extra_headers(self) -> dict:
        return {"Referer": REFERER, "User-Agent": UA}

    def prepare_order(self, *, isin: str, side: int, config_section: dict) -> PreparedOrder:
        """FIRING prep: login → size → create N drafts → return the BATCH order.

        Creates the server-side drafts NOW (off the hot path) and returns a
        :class:`PreparedOrder` whose ``order_url`` is the batchCreate endpoint and
        ``body`` is the batch payload referencing the new draft ids — the firer
        just re-POSTs it until the first success. **Has a side effect** (drafts);
        ``validate`` is the no-draft warmup path.
        """
        side = int(side)
        if side not in (1, 2):
            raise ValueError(f"mofid {isin}: invalid order side {side!r} (expected 1 or 2)")
        self.config_section = config_section if config_section is not None else self.config_section
        try:
            descriptor = self._session()
            price, volume = self._size(isin, side, descriptor)

            n = max(1, runtime_config.get_int("mofid_draft_count", DRAFT_COUNT_DEFAULT))
            draft_body = self._draft_body(isin, side, price, volume)
            draft_ids: list = []
            for _ in range(n):
                rd = _READ_SESSION.post(
                    self._draft_url(),
                    json=draft_body,
                    headers={
                        "Authorization": f"Bearer {descriptor['token']}",
                        "Content-Type": "application/json", "Accept": "application/json",
                        **self._extra_headers(),
                    },
                    timeout=TIMEOUT,
                )
                try:
                    did = (rd.json() or {}).get("id")
                except Exception:  # noqa: BLE001
                    did = None
                if did is not None:
                    draft_ids.append(did)
                else:
                    logger.warning(
                        "mofid draft create failed (%s@%s %s): HTTP %s %s",
                        self.username, self.broker_code, isin, rd.status_code, rd.text[:120],
                    )
            if not draft_ids:
                raise RuntimeError(
                    f"mofid: no drafts created for {self.username}@{self.broker_code} {isin}"
                )
            batch = {"draftIds": draft_ids, "removeDraftAfterCreate": False, "orderFrom": 34}
            logger.info(
                "mofid %s (%s@%s): %d draft(s) → batch, price=%s vol=%s",
                isin, self.username, self.broker_code, len(draft_ids), price, f"{volume:,}",
            )
            return PreparedOrder(
                order_url=self._batch_url(),
                body=json.dumps(batch),
                bearer_token=descriptor["token"],
                signer=None,
                cookies=None,
                price=price,
                volume=volume,
                extra_headers=self._extra_headers(),
            )
        except (ValueError, InvalidCredentialsError):
            raise
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"mofid prepare_order failed for {self.username}@{self.broker_code} "
                f"isin={isin} side={side}: {e}"
            ) from e

    def validate(self, *, isin: str, side: int, config_section: dict) -> PreparedOrder:
        """WARMUP health check: login + sizing, NO drafts (no side effect).

        Returns a :class:`PreparedOrder` carrying the computed price/volume (so
        cache_warmup can log them) but pointing at the immediate-order URL with a
        single-order body — it is NEVER fired by the warmup, which only reads
        ``.price``/``.volume``.
        """
        side = int(side)
        if side not in (1, 2):
            raise ValueError(f"mofid {isin}: invalid order side {side!r} (expected 1 or 2)")
        self.config_section = config_section if config_section is not None else self.config_section
        try:
            descriptor = self._session()
            price, volume = self._size(isin, side, descriptor)
            return PreparedOrder(
                order_url=self._order_url(),
                body=json.dumps(self._single_order_body(isin, side, price, volume)),
                bearer_token=descriptor["token"],
                signer=None, cookies=None, price=price, volume=volume,
                extra_headers=self._extra_headers(),
            )
        except (ValueError, InvalidCredentialsError):
            raise
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"mofid validate failed for {self.username}@{self.broker_code} "
                f"isin={isin} side={side}: {e}"
            ) from e

    def _single_order_body(self, isin: str, side: int, price: float, volume: int) -> dict:
        return {
            "order": {
                "orderFrom": 34,
                "price": str(int(price)),
                "quantity": str(int(volume)),
                "side": _mofid_side(side),
                "symbolIsin": isin,
                "validityType": 0,
            }
        }

    def open_sell_context(self, *, isin: str, config_section: dict) -> SellContext:
        """Auto-sell (#110): floor = RLC ``lap``, cap = ``mxqo``. SELL chunks use
        the single immediate ``/core/api/v2/order`` (spaced ≥0.35s — far from any
        burst, so the draft/batch overhead buys nothing here)."""
        self.config_section = config_section if config_section is not None else self.config_section
        override = self.config_section.get("price")
        if override:
            floor = int(float(override))
        else:
            _ceiling, floor = rlc_price.get_price_band(isin)
            floor = int(floor)
        if floor <= 0:
            raise ValueError(f"no rlc price band (floor) for {isin}")
        cap = int(rlc_price.get_max_order_qty(isin) or 0)
        self._session()  # fail-fast on bad creds now, not at first trigger

        def fetch_holdings() -> int:
            return int(self._holdings(isin, self._session()) or 0)

        def prepare_chunk(volume: int) -> PreparedOrder:
            d = self._session()
            return PreparedOrder(
                order_url=self._order_url(),
                body=json.dumps(self._single_order_body(isin, 2, floor, int(volume))),
                bearer_token=d["token"],
                signer=None, cookies=None, price=floor, volume=int(volume),
                extra_headers=self._extra_headers(),
            )

        return SellContext(
            floor_price=floor,
            max_order_volume=cap,
            fetch_holdings=fetch_holdings,
            prepare_chunk=prepare_chunk,
        )
