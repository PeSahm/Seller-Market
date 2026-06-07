"""Exir / Rayan-HamAfza broker adapter (Phase 2 of the Exir feature).

Unlike ephoenix (static Bearer header), Exir authenticates with a cookie session
plus a per-request, second-granular ``X-App-N`` signature computed from the login
``nt`` seed. Exir streams live prices over Lightstreamer and has no per-instrument
REST price endpoint of its own, but the broker's *own* RLC market-data backend
exposes a public band handler — see :mod:`rlc_price`. The daily allowed price band
therefore comes from the broker's infrastructure (no tsetmc, no cross-VPS relay),
reachable directly from each trading host. BUY fires at the ceiling, SELL at the
floor.

Confirmed live (Phase-0 spike against khobregan — see
``scratch/EXIR_FINDINGS.md``):

* ``GET /exir`` bootstraps ``cookiesession1``.
* ``GET /captcha`` returns a JPEG + a ``client_login_id`` JWT header/cookie; the
  OCR service decodes it to 5 numeric digits.
* ``POST /api/v2/login`` JSON ``{"username","password","captcha":<int>,"otp":""}``
  → ``nt`` (signing seed), ``authToken`` (JWT; ``"b"`` claim = numeric broker id),
  ``validity`` (minutes), session cookies.
* Reads use ``X-App-N`` over the full path+query, UTC clock.
* ``GET /api/v1/user/stockInfo`` → ``purchaseUpperBound`` — the buying-power /
  account-credit endpoint the web app uses (the bare ``/api/v2/user/buyingPower``
  406s for this account; ``stockInfo`` returns the same field the decompiled
  ``GetBalance`` read).
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

import rlc_price
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


def _decode_broker_id(
    rlc_token: Optional[str],
    auth_token: Optional[str],
    response_username: Optional[str],
    login_username: str,
) -> Any:
    """Extract the numeric broker id for the order payload's ``brokerCode``.

    CONFIRMED live: the ``"b"`` claim (e.g. khobregan → ``116``) lives in the
    ``rlcAuthHeader`` JWT, NOT the ``authToken`` (which carries no ``b``). We try
    rlcAuthHeader, then authToken, then derive it from the broker-prefixed login
    *response* username (``"1164580090306"`` == ``116`` + the account
    ``"4580090306"``). Returns ``None`` only if nothing is recoverable.
    """
    for tok in (rlc_token, auth_token):
        if not tok:
            continue
        try:
            parts = tok.split(".")
            if len(parts) >= 2:
                b = _b64url_json(parts[1]).get("b")
                if b is not None:
                    return b
        except Exception as e:  # noqa: BLE001 — fall through to the username heuristic
            logger.debug(f"exir: broker-id JWT 'b' decode failed ({e})")

    # Derive from the response username = brokerId + account (strip the account
    # we logged in with). E.g. "1164580090306" minus "4580090306" → "116".
    ru, lu = str(response_username or ""), str(login_username or "")
    if ru and lu and ru != lu and ru.endswith(lu):
        prefix = ru[: len(ru) - len(lu)]
        if prefix.isdigit():
            return int(prefix)
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
                broker_id = _decode_broker_id(
                    login_json.get("rlcAuthHeader"),
                    login_json.get("authToken"),
                    login_json.get("username"),
                    self.username,
                )
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
        """Spendable cash via ``GET /api/v1/user/stockInfo`` → ``purchaseUpperBound``.

        (The bare ``/api/v2/user/buyingPower`` 406s for this account; ``stockInfo``
        is the account-credit endpoint the web app uses and carries the same
        ``purchaseUpperBound`` the decompiled ``GetBalance`` read.)
        """
        data = self._get("/api/v1/user/stockInfo", descriptor)
        return float(data.get("purchaseUpperBound") or 0)

    def _buy_fee_rate(self, isin: str, descriptor: dict) -> float:
        """Per-instrument BUY commission rate via ``GET /api/v2/wages/instrument/{isin}``.

        Response shape: ``{"<isin>": {"SIDE_BUY": 0.003712, "SIDE_SALE": 0.0088}}``.
        The bot needs this so the BUY volume is fee-adjusted (the ephoenix family
        gets the same from the broker's ``CalculateOrderParam``); without it, the
        order would over-spend the buying power and the broker would reject it.
        """
        data = self._get(f"/api/v2/wages/instrument/{isin}", descriptor)
        entry = (data or {}).get(isin) or {}
        return float(entry.get("SIDE_BUY") or 0.0)

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

        # The daily allowed price band comes from the broker's own RLC market-data
        # gateway (see rlc_price) — direct, no tsetmc, no cross-VPS relay: BUY fires
        # at the ceiling (limit-up), SELL at the floor (limit-down) to sit
        # head-of-queue. A config `price` is honoured only as an explicit override.
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

            if side == 1:  # BUY — size from spendable cash, NET OF the buy fee.
                bp = self._buying_power(descriptor)
                fee = self._buy_fee_rate(isin, descriptor)
                # floor(BP / (price * (1 + buyFee))) — mirrors the ephoenix
                # CalculateOrderParam fee-adjustment so the order can't over-spend
                # the buying power (which the broker would reject).
                volume = int(bp / (price * (1.0 + fee)))
                logger.info(
                    f"exir BUY {isin} ({self.username}@{self.broker_code}): "
                    f"bp={bp:,.0f}, price={price}, fee={fee}, raw_volume={volume:,}"
                )
            else:  # SELL — size from real holdings; fail-fast on nothing held.
                volume = self._holdings(isin, descriptor)
                if volume <= 0:
                    raise ValueError(f"no Exir holdings for {isin}")
                logger.info(
                    f"exir SELL {isin} ({self.username}@{self.broker_code}): "
                    f"holdings/raw_volume={volume:,}"
                )

            # Cap at the instrument's MAX ORDER QUANTITY (RLC ``mxqo``). The
            # broker rejects any single order whose volume exceeds it ("volume
            # upper threshold"). Applies to BUY (BP-derived — can blow past the
            # cap for a large account on a cheap stock) AND SELL (large holdings).
            max_qty = rlc_price.get_max_order_qty(isin)
            if max_qty and 0 < max_qty < volume:
                logger.warning(
                    f"exir {isin} ({self.username}@{self.broker_code}): volume "
                    f"{volume:,} exceeds max order qty {max_qty:,} — capping"
                )
                volume = max_qty
            # Operator-set hard cap (optional), applied last.
            max_volume = config_section.get("max_volume")
            if max_volume:
                volume = min(volume, int(max_volume))
            if volume <= 0:
                raise ValueError(
                    f"exir {isin}: computed order volume is 0 (bp/holdings too small)"
                )
            logger.info(
                f"exir {isin} ({self.username}@{self.broker_code}): "
                f"final volume={volume:,} (max_order_qty={max_qty or 'n/a'})"
            )

            broker_id = descriptor["broker_id"]
            if broker_id is None:
                # _decode_broker_id exhausted every source — never POST a null
                # brokerCode (the broker would mis-route/reject it). Fail-fast
                # off the hot path so it shows in the run summary.
                raise ValueError(
                    f"Exir broker_id unresolved for {self.username}@{self.broker_code}; "
                    "cannot build order"
                )
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
