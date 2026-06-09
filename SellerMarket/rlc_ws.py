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
  ``MW,<insCode>,<ISIN>,<name>,...`` — the queue numbers are positional. A ``V,...``
  frame carries the server time. (The SPA's ``parseMessage`` JSON.parse is a
  different code path; the live wire is CSV.)

The exact MW field INDEX carrying the buy-queue count needs live data to pin
(at market close every queue field is 0 → ambiguous). So this client
**self-calibrates**: on a live MW frame it finds the index whose value equals the
REST best-buy queue (``rlc_market.get_queue(isin)['buy_volume']`` == ``bbq``) and
locks onto it — but ONLY when that match is unambiguous. One socket PER ISIN, so
frames need no ISIN-routing field (still verified against ``parts[2]``).

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import base64
import logging
import threading
from typing import Callable, Optional

import requests

import rlc_market

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


def find_buy_queue_index(parts: list[str], expected_bbq: int) -> Optional[int]:
    """Return the field INDEX whose int value == ``expected_bbq``, IFF unambiguous.

    Self-calibrates which positional MW field is the best-buy queue against the
    REST cross-check. A tie (two fields share the value — common when the queue
    is 0 at market close) returns None so the caller waits for a clean frame;
    binding the wrong index would corrupt every later sell decision.
    """
    if expected_bbq is None or expected_bbq <= 0:
        # 0/None can't disambiguate (many fields are 0) — never calibrate on it.
        return None
    candidates = [
        i for i, p in enumerate(parts)
        if p and p.lstrip("-").isdigit() and int(p) == int(expected_bbq)
    ]
    return candidates[0] if len(candidates) == 1 else None


def extract_index(parts: list[str], index: int) -> Optional[int]:
    """Read a numeric field at ``index`` from the CSV parts, or None."""
    try:
        v = parts[index]
        return int(v) if v and v.lstrip("-").isdigit() else None
    except (IndexError, TypeError, ValueError):
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
        """Return (push_url, rlc_auth), logging in (and caching) if needed."""
        with self._auth_lock:
            if force or not self._rlc_auth or not self._push_urls:
                self._push_urls, self._rlc_auth, self._cookies = login(
                    self.tenant, self.username, self.password, self.decode_captcha)
                self._rr = 0
            url = self._push_urls[self._rr % len(self._push_urls)]
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

        index: Optional[int] = None
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
                    parts = parse_mw(ws.recv())
                    if parts is None:
                        continue
                    # One socket per ISIN, but verify the frame is ours.
                    if len(parts) > 2 and parts[2] and parts[2] != isin:
                        continue
                    if index is None:
                        rest = rlc_market.get_queue(isin) or {}
                        index = find_buy_queue_index(parts, rest.get("buy_volume"))
                        if index is not None:
                            logger.info("rlc_ws %s: calibrated buy-queue index=%d", isin, index)
                        else:
                            continue  # market closed / ambiguous — wait for a clean frame
                    self.on_update(isin, extract_index(parts, index))
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


__all__ = ["RlcQueueClient", "login", "parse_mw",
           "find_buy_queue_index", "extract_index"]
