"""Exir / Rayan-HamAfza broker adapter (Phase 2 of the Exir feature).

Unlike ephoenix (static Bearer header), Exir authenticates with a cookie session
plus a per-request, second-granular ``X-App-N`` signature computed from the login
``nt`` seed. There is NO instrument-price endpoint, so the limit price is carried
in config.

Confirmed live (Phase-0 spike against khobregan — see
``scratch/EXIR_FINDINGS.md``):

* ``GET /exir`` bootstraps ``cookiesession1``.
* ``GET /captcha`` returns a JPEG + a ``client_login_id`` JWT header/cookie; the
  OCR service decodes it to 5 numeric digits.
* ``POST /api/v2/login`` JSON ``{"username","password","captcha":<int>,"otp":""}``
  → ``nt`` (signing seed), ``authToken`` (JWT; ``"b"`` claim = numeric broker id),
  ``validity`` (minutes), session cookies.
* Reads use ``X-App-N`` over the full path+query, UTC clock.
* ``GET /api/v1/user/asset`` → ``{"accountNumber","asset"}`` (asset = spendable
  cash) — the working buying-power endpoint (the spike's ``/api/v2/user/buyingPower``
  returned a 406 business error).
* ``GET /api/v1/user/portfoReport`` → ``{"result":[{... insMaxLcode:<ISIN>,
  asset/remainQty:<qty>}]}`` for holdings.
* Order placement: ``POST /api/v1/order`` with the symbol/ISIN-keyed body below.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Callable, Optional

import requests

from broker_adapters import BrokerAdapter, PreparedOrder
from captcha_utils import decode_captcha as _default_decode_captcha
from exir_token import build_app_n, make_signer, pw_fingerprint

logger = logging.getLogger(__name__)

CAPTCHA_RETRIES = 6
TIMEOUT = 20
_MIN_TTL_SECONDS = 60  # clamp the login `validity` so a tiny/absent value can't churn logins

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Module-level session cache, keyed by (broker_code, username, pw_fingerprint).
# Each entry: {"cookies": dict, "nt": str, "broker_id": Any, "expires_at": float}
# `expires_at` is a time.monotonic() deadline so a clock change can't extend TTL.
_SESSION_CACHE: dict = {}


def _b64url_json(segment: str) -> dict:
    """Decode one base64url JWT segment (no signature check) to a dict."""
    pad = "=" * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(segment + pad)
    return json.loads(raw.decode("utf-8", "replace"))


def _decode_broker_id(auth_token: Optional[str], username: str) -> Any:
    """Extract the numeric broker id (``"b"`` claim) from the ``authToken`` JWT.

    Best-effort: on any decode failure, fall back to the leading numeric prefix
    of the username (Exir usernames are prefixed with the broker id, e.g.
    ``"116..."`` → ``116``). Returns ``None`` only if nothing is recoverable.
    """
    if auth_token:
        try:
            parts = auth_token.split(".")
            if len(parts) >= 2:
                claims = _b64url_json(parts[1])
                b = claims.get("b")
                if b is not None:
                    return b
        except Exception as e:  # noqa: BLE001 — fall through to username heuristic
            logger.debug(f"exir: authToken 'b' decode failed ({e}); falling back to username prefix")

    # Fallback: leading digit run of the username.
    digits = ""
    for ch in str(username):
        if ch.isdigit():
            digits += ch
        else:
            break
    if digits:
        try:
            return int(digits)
        except ValueError:
            return digits
    return None


class ExirAdapter(BrokerAdapter):
    """Adapter for the Exir / Rayan-HamAfza broker family."""

    family = "exir"

    def __init__(
        self,
        broker_code: str,
        username: str,
        password: str,
        captcha_decoder: Optional[Callable[[str], str]] = None,
        cache: Optional[Any] = None,
    ):
        self.broker_code = broker_code
        self.username = username
        self.password = password
        # Allow injection for tests; default to the shared OCR helper.
        self.captcha_decoder = captcha_decoder or _default_decode_captcha
        self.cache = cache  # unused for exir (no shared cache schema), kept for signature parity
        self.base = f"https://{broker_code}.exirbroker.com"

    # ---- auth / session ---------------------------------------------------

    def _cache_key(self) -> tuple:
        return (self.broker_code, self.username, pw_fingerprint(self.password))

    def _login(self, session: requests.Session) -> dict:
        """Log in and return a session descriptor.

        Returns ``{"cookies","nt","broker_id","expires_at"}``. Raises
        ``RuntimeError`` if captcha/login never succeeds.
        """
        # Step 1: cookie bootstrap.
        session.get(self.base + "/exir", timeout=TIMEOUT)

        # Step 2 + 3: captcha → OCR → login, retried.
        for attempt in range(1, CAPTCHA_RETRIES + 1):
            rc = session.get(self.base + "/captcha", timeout=TIMEOUT)
            client_login_id = rc.headers.get("client_login_id")
            if client_login_id:
                session.cookies.set("client_login_id", client_login_id)
            b64 = base64.b64encode(rc.content).decode()
            cap = self.captcha_decoder(b64)
            logger.debug(
                f"exir login attempt {attempt}: captcha bytes={len(rc.content)} "
                f"ocr_len={len(cap) if cap else 0}"
            )
            if not (cap and cap.isdigit() and len(cap) == 5):
                continue

            login_body = {
                "username": self.username,
                "password": self.password,
                "captcha": int(cap),  # captcha is a JSON NUMBER, not a string
                "otp": "",
            }
            rl = session.post(
                self.base + "/api/v2/login",
                json=login_body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=TIMEOUT,
            )
            try:
                login_json = rl.json()
            except Exception:
                logger.debug(f"exir login: non-JSON response (HTTP {rl.status_code})")
                continue

            nt = login_json.get("nt")
            if nt:
                auth_token = login_json.get("authToken")
                broker_id = _decode_broker_id(auth_token, self.username)
                validity_min = login_json.get("validity") or 0
                try:
                    ttl = max(_MIN_TTL_SECONDS, int(float(validity_min)) * 60)
                except (TypeError, ValueError):
                    ttl = _MIN_TTL_SECONDS
                descriptor = {
                    "cookies": dict(session.cookies),
                    "nt": nt,
                    "broker_id": broker_id,
                    "expires_at": time.monotonic() + ttl,
                }
                logger.info(
                    f"exir login ok for {self.username}@{self.broker_code} "
                    f"(broker_id={broker_id}, ttl={ttl}s)"
                )
                return descriptor

            logger.debug(
                f"exir login attempt {attempt}: HTTP {rl.status_code}, no `nt` in response"
            )

        raise RuntimeError(
            f"Exir login failed for {self.username}@{self.broker_code} "
            f"after {CAPTCHA_RETRIES} captcha attempts"
        )

    def _session(self) -> dict:
        """Return a valid session descriptor, logging in (and caching) if needed."""
        key = self._cache_key()
        entry = _SESSION_CACHE.get(key)
        if entry and entry.get("expires_at", 0) > time.monotonic():
            return entry

        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Accept": "*/*"})
        descriptor = self._login(session)
        _SESSION_CACHE[key] = descriptor
        return descriptor

    # ---- signed reads -----------------------------------------------------

    def _get(self, path: str, descriptor: dict) -> Any:
        """Signed ``GET base+path`` → parsed JSON. Raises on transport/HTTP error."""
        headers = {
            "X-App-N": build_app_n(descriptor["nt"], path),
            "Accept": "application/json",
        }
        resp = requests.get(
            self.base + path,
            headers=headers,
            cookies=descriptor["cookies"],
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _buying_power(self, descriptor: dict) -> float:
        """Spendable cash via ``GET /api/v1/user/asset`` → ``json["asset"]``."""
        data = self._get("/api/v1/user/asset", descriptor)
        return float(data["asset"])

    def _holdings(self, isin: str, descriptor: dict) -> int:
        """Whole-share holdings for ``isin`` via ``GET /api/v1/user/portfoReport``."""
        data = self._get("/api/v1/user/portfoReport", descriptor)
        rows = data.get("result") or []
        for row in rows:
            code = row.get("insMaxLcode") or row.get("insMaxLCode")
            if code == isin:
                qty = row.get("asset")
                if qty is None:
                    qty = row.get("remainQty")
                return int(qty or 0)
        return 0

    # ---- order prep -------------------------------------------------------

    def prepare_order(self, *, isin: str, side: int, config_section: dict) -> PreparedOrder:
        """Prepare one Exir order; may raise on any auth/sizing/config failure."""
        side = int(side)
        config_section = config_section or {}

        # Price is REQUIRED from config — Exir exposes no instrument-price endpoint.
        price = float(config_section.get("price") or 0)
        if price <= 0:
            raise ValueError(
                "Exir order requires a 'price' in config (no instrument price endpoint)"
            )

        try:
            descriptor = self._session()

            if side == 1:  # BUY — size from spendable cash.
                bp = self._buying_power(descriptor)
                volume = int(bp // price)
                max_volume = config_section.get("max_volume")
                if max_volume:
                    volume = min(volume, int(max_volume))
                logger.info(
                    f"exir BUY {isin} ({self.username}@{self.broker_code}): "
                    f"bp={bp:,.0f}, price={price}, volume={volume:,}"
                )
            else:  # SELL — size from real holdings; fail-fast on nothing held.
                volume = self._holdings(isin, descriptor)
                if volume <= 0:
                    raise ValueError(f"no Exir holdings for {isin}")
                logger.info(
                    f"exir SELL {isin} ({self.username}@{self.broker_code}): "
                    f"holdings/volume={volume:,}"
                )

            broker_id = descriptor["broker_id"]
            body = json.dumps({
                "bankAccountId": -1,
                "brokerCode": broker_id,
                "disclosedQuantity": 0,
                "hasUnderCautionAgreement": True,
                "insMaxLcode": isin,
                "orderType": "ORDER_TYPE_LIMIT",
                "price": str(price),
                "quantity": str(volume),
                "side": ("SIDE_BUY" if side == 1 else "SIDE_SALE"),
                "validityType": "VALIDITY_TYPE_DAY",
                "coreType": "c",
            })

            signer = make_signer(descriptor["nt"], "/api/v1/order")

            return PreparedOrder(
                order_url=self.base + "/api/v1/order",
                body=body,
                bearer_token=None,
                signer=signer,
                cookies=descriptor["cookies"],
                price=price,
                volume=volume,
            )
        except ValueError:
            # Domain errors (missing price / no holdings) propagate as-is.
            raise
        except Exception as e:
            # Any network/auth/parse failure → a clear, actionable exception.
            # The caller marks the locust user failed, same as ephoenix auth failure.
            raise RuntimeError(
                f"Exir prepare_order failed for {self.username}@{self.broker_code} "
                f"isin={isin} side={side}: {e}"
            ) from e
