"""Upstream RLC/Exir market-data WebSocket client (#110, Phase 1).

The shared market-data service (PouyanIt) holds ONE Khobregan-authenticated
connection to the Exir live stream and re-broadcasts each instrument's best-buy
queue to local bots. This is the UPSTREAM half: it logs in (captcha→OCR→login),
opens the WebSocket, subscribes an instrument's Market-Watch channel, and calls
``on_update(isin, buy_volume)`` per push.

Wire shape — **confirmed live** (PouyanIt, 2026-06; corrected from the SPA bundle,
see ``scratch/RLC_WS_FINDINGS.md``):

* WS host = one of the login response's ``pushServerUrls`` (e.g.
  ``push103.irbroker.com``), NOT the tenant host. Full URL:
  ``wss://<pushServerUrl>/v2/ws?encoding=text&authToken=<rlcAuthHeader>&device=web``
* Auth param = the login response's ``rlcAuthHeader`` (the JWT with the broker
  claim), NOT the login ``authToken``.
* Subscribe with the text frame ``"1,MW.<ISIN>"`` (MW = Market Watch).
* Inbound frames are **comma-separated text** (NOT JSON):
  ``MW,<insCode>,<ISIN>,<name>,...``. A ``V,...`` frame carries the server time,
  ``N2,...`` a last-price tick. (The SPA's ``parseMessage`` JSON.parse is a
  different code path; the live wire is CSV.)
* **The order-book depth is NOT a flat CSV field** — it rides in semicolon-packed
  level blobs appended to the MW frame (confirmed live 2026-06-10 on
  IRT3SORF0001; value == the REST ``bbq``)::

      bl1;<buyVol>;<buyPrice>;<buyCount>;<buyTime>;<sellVol>;<sellPrice>;<sellCount>;<sellTime>

  ``bl2``/``bl3`` are the deeper levels. The best-buy-queue share count is
  ``bl1`` element 1 — parsed explicitly by :func:`extract_buy_queue`. (The
  earlier flat-field self-calibration could NEVER match: the volume only ever
  appears inside the blob, so the index never bound and no update ever flowed.)
* **An idle socket is NORMAL**: the server pushes an MW snapshot on subscribe,
  then only on order-book CHANGES (a quiet instrument can go minutes between
  frames). A recv timeout is benign — ping + keep reading, never tear down.
  Reconnecting needlessly also burns the auth: a ``rlcAuthHeader`` is
  single-use per push host (``401 token already has been used``), forcing a
  fresh captcha→login each cycle.

One socket PER ISIN, so frames need no ISIN-routing field (still verified
against ``parts[2]``).

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import base64
import logging
import threading
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

CAPTCHA_RETRIES = 6
TIMEOUT = 20
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def login(tenant: str, username: str, password: str,
          decode_captcha: Callable[[str], str]) -> tuple[list[str], str, dict]:
    """captcha → OCR → ``POST /api/v2/login`` → ``(push_urls, rlc_auth_header, cookies)``.

    ``push_urls`` is the response's ``pushServerUrls`` (the WS hosts) and
    ``rlc_auth_header`` is ``rlcAuthHeader`` (the WS ``authToken`` query param).
    Raises ``RuntimeError`` if the OCR/login never succeeds. Direct (``trust_env=
    False``) — reach the Iranian broker without a foreign proxy.
    """
    base = f"https://{tenant}.exirbroker.com"
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": _UA, "Accept": "*/*"})
    s.get(base + "/exir", timeout=TIMEOUT)
    for attempt in range(1, CAPTCHA_RETRIES + 1):
        rc = s.get(base + "/captcha", timeout=TIMEOUT)
        cli = rc.headers.get("client_login_id")
        if cli:
            s.cookies.set("client_login_id", cli)
        cap = decode_captcha(base64.b64encode(rc.content).decode())
        if cap and cap.isdigit() and len(cap) == 5:
            rl = s.post(
                base + "/api/v2/login",
                json={"username": username, "password": password,
                      "captcha": int(cap), "otp": ""},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=TIMEOUT,
            )
            try:
                body = rl.json() or {}
            except Exception:  # noqa: BLE001
                body = {}
            push_urls = body.get("pushServerUrls")
            rlc_auth = body.get("rlcAuthHeader")
            if push_urls and rlc_auth:
                # De-dup while preserving order.
                seen, urls = set(), []
                for u in push_urls:
                    if u and u not in seen:
                        seen.add(u)
                        urls.append(u)
                return urls, rlc_auth, dict(s.cookies)
        logger.debug("rlc_ws login attempt %d: no pushServerUrls/rlcAuthHeader yet", attempt)
    raise RuntimeError(f"rlc_ws login failed for {username}@{tenant}")


def parse_mw(raw) -> Optional[list[str]]:
    """Return the comma-split fields of a Market-Watch frame, else None.

    Only ``MW,...`` frames carry instrument data; ``V,...`` (server time) and any
    binary/other frame are ignored.
    """
    if not isinstance(raw, str) or not raw.startswith("MW,"):
        return None
    return raw.split(",")


def extract_buy_queue(parts: list[str]) -> Optional[int]:
    """Best-buy-queue share count from a Market-Watch frame, or None.

    The MW frame appends the order-book depth as semicolon-packed levels;
    level 1 is the ``bl1;…`` field and its element 1 is the buy-side share
    count (live-verified == the REST ``bbq``). Returns None when no parseable
    ``bl1`` exists (malformed frame / depth missing) — the consumer treats
    None as HOLD, never as "sell". A literal ``bl1;0;…`` returns 0: an empty
    buy queue IS the thinned-queue condition.
    """
    for p in parts:
        if isinstance(p, str) and p.startswith("bl1;"):
            bits = p.split(";")
            if len(bits) > 1 and bits[1].isdigit():
                return int(bits[1])
            return None  # malformed level blob — fail-safe HOLD
    return None


class RlcQueueClient:
    """One Khobregan login shared across one upstream WS per subscribed ISIN."""

    def __init__(self, tenant: str, username: str, password: str,
                 decode_captcha: Callable[[str], str],
                 on_update: Callable[[str, Optional[int]], None], *,
                 reconnect_min: float = 1.0, reconnect_max: float = 30.0):
        self.tenant = tenant
        self.username = username
        self.password = password
        self.decode_captcha = decode_captcha
        self.on_update = on_update
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._push_urls: list[str] = []
        self._rlc_auth: Optional[str] = None
        self._cookies: dict = {}
        self._auth_lock = threading.Lock()
        self._rr = 0  # round-robin index across push servers

    def _ensure_auth(self, force: bool = False) -> tuple[str, str]:
        """Return (push_url, rlc_auth), logging in (and caching) if needed.

        Each call hands out the NEXT push host: the rlcAuthHeader is
        single-use PER HOST (a reused token gets ``401 token already has been
        used``), so concurrent per-ISIN threads sharing one login must spread
        their handshakes across hosts. With more subscribed ISINs than push
        hosts the surplus handshake 401s and the existing force-re-login path
        mints a fresh token.
        """
        with self._auth_lock:
            if force or not self._rlc_auth or not self._push_urls:
                self._push_urls, self._rlc_auth, self._cookies = login(
                    self.tenant, self.username, self.password, self.decode_captcha)
                self._rr = 0
            url = self._push_urls[self._rr % len(self._push_urls)]
            self._rr += 1
            return url, self._rlc_auth

    def _next_push(self) -> None:
        with self._auth_lock:
            self._rr += 1

    def subscribe(self, isin: str) -> None:
        if isin in self._threads:
            return
        t = threading.Thread(target=self._run_one, args=(isin,), daemon=True,
                             name=f"rlc-ws-{isin}")
        self._threads[isin] = t
        t.start()

    def _run_one(self, isin: str) -> None:
        import websocket  # websocket-client

        first_value_logged = False
        backoff = self._reconnect_min
        while not self._stop.is_set():
            ws = None
            try:
                push_url, rlc_auth = self._ensure_auth()
                url = (f"wss://{push_url}/v2/ws"
                       f"?encoding=text&authToken={rlc_auth}&device=web")
                cookie = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
                # Default TLS verification ON — the WS carries the rlcAuthHeader.
                ws = websocket.create_connection(
                    url,
                    header=[f"User-Agent: {_UA}",
                            f"Origin: https://{self.tenant}.exirbroker.com"],
                    cookie=cookie,
                    timeout=15,
                )
                ws.send("1,MW." + isin)
                logger.info("rlc_ws %s: subscribed via %s", isin, push_url)
                backoff = self._reconnect_min
                ws.settimeout(40)
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        # Idle is NORMAL — the server only pushes on order-book
                        # changes. Ping to keep the socket/NAT alive (a dead
                        # socket makes ping raise → the outer reconnect path).
                        ws.ping()
                        continue
                    parts = parse_mw(raw)
                    if parts is None:
                        continue
                    # One socket per ISIN, but verify the frame is ours.
                    if len(parts) > 2 and parts[2] and parts[2] != isin:
                        continue
                    value = extract_buy_queue(parts)
                    if value is None:
                        continue  # frame without a parseable depth level — HOLD
                    if not first_value_logged:
                        logger.info("rlc_ws %s: first buy-queue value=%d", isin, value)
                        first_value_logged = True
                    self.on_update(isin, value)
            except Exception as exc:  # noqa: BLE001 — disconnect/auth → HOLD + reconnect
                logger.warning("rlc_ws %s: disconnected: %s", isin, exc)
                self.on_update(isin, None)         # fail-safe HOLD
                if "401" in str(exc) or "403" in str(exc):
                    try:
                        self._ensure_auth(force=True)
                    except Exception:  # noqa: BLE001 — re-auth failure must not kill the worker
                        logger.exception("rlc_ws %s: forced re-auth failed", isin)
                else:
                    self._next_push()              # try the next push server
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self._reconnect_max)
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self) -> None:
        self._stop.set()


__all__ = ["RlcQueueClient", "login", "parse_mw", "extract_buy_queue"]
