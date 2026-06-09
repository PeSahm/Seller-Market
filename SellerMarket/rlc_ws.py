"""Upstream RLC/Exir market-data WebSocket client (#110, Phase 1).

The per-host market-data service (PouyanIt) holds ONE Khobregan-authenticated
connection to the Exir live stream and re-broadcasts each instrument's best-buy
queue to local bots. This module is the UPSTREAM half: it logs in (captcha→OCR→
login → ``authToken``), opens the WebSocket, subscribes an instrument's
Market-Watch channel, and calls ``on_update(isin, buy_volume)`` per push.

Wire shape (cracked from the SPA bundle — see ``scratch/RLC_WS_FINDINGS.md``):

    wss://<tenant>.exirbroker.com/sle/v2/ws?encoding=text&authToken=<token>&device=web
    subscribe with the text frame:  "1,MW.<ISIN>"
    inbound frames are JSON: {"msgType": ..., ...}

The exact MW-frame field that carries the buy-queue count is NOT pinned yet
(market-hours probe). So this client **self-calibrates**: on the first numeric
MW frame for an ISIN it finds the field whose value equals the REST best-buy
queue (``rlc_market.get_queue(isin)['buy_volume']`` == ``bbq``) and locks onto
it. One socket PER ISIN, so frames need no ISIN-routing field.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
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
# msgTypes that carry no market data — never calibrate / extract from these.
_CONTROL_MSGTYPES = {"connect", "time", "ping", "pong", "heartbeat", None}


def login(tenant: str, username: str, password: str,
          decode_captcha: Callable[[str], str]) -> tuple[str, dict]:
    """captcha → OCR → ``POST /api/v2/login`` → ``(authToken, cookies)``.

    Raises ``RuntimeError`` if the OCR/login never succeeds. Mirrors the proven
    ``exir_adapter._login`` flow (kept here so the sidecar doesn't drag in the
    full adapter / its session cache).
    """
    base = f"https://{tenant}.exirbroker.com"
    s = requests.Session()
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
                token = (rl.json() or {}).get("authToken")
            except Exception:  # noqa: BLE001
                token = None
            if token:
                return token, dict(s.cookies)
        logger.debug("rlc_ws login attempt %d: no token yet", attempt)
    raise RuntimeError(f"rlc_ws login failed for {username}@{tenant}")


def find_buy_queue_field(frame: dict, expected_bbq: int) -> Optional[str]:
    """Return the field whose numeric value == ``expected_bbq``, IFF unambiguous.

    Self-calibrates which MW-frame key is the best-buy queue against the REST
    cross-check. If two or more fields share that value in one frame the match is
    ambiguous, so we return ``None`` and the caller waits for a cleaner frame —
    binding the wrong field would corrupt every later sell decision. Dotted
    ``"a.b"`` for a one-level-nested match.
    """
    if expected_bbq is None:
        return None
    candidates: list[str] = []
    for k, v in frame.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and int(v) == int(expected_bbq):
            candidates.append(k)
    for k, v in frame.items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                if isinstance(vv, bool):
                    continue
                if isinstance(vv, (int, float)) and int(vv) == int(expected_bbq):
                    candidates.append(f"{k}.{kk}")
    return candidates[0] if len(candidates) == 1 else None


def extract_field(frame: dict, field: str) -> Optional[int]:
    """Read a (possibly one-level-dotted) numeric field from a frame, or None."""
    try:
        if "." in field:
            outer, inner = field.split(".", 1)
            v = (frame.get(outer) or {}).get(inner)
        else:
            v = frame.get(field)
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError, AttributeError):
        return None


def parse_frame(raw: str) -> Optional[dict]:
    """JSON-decode one inbound frame; None for non-JSON or control frames."""
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("msgType") in _CONTROL_MSGTYPES:
        return None
    return obj


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
        self._token: Optional[str] = None
        self._cookies: dict = {}
        self._auth_lock = threading.Lock()

    def _ensure_auth(self, force: bool = False) -> str:
        with self._auth_lock:
            if force or not self._token:
                self._token, self._cookies = login(
                    self.tenant, self.username, self.password, self.decode_captcha)
            return self._token

    def subscribe(self, isin: str) -> None:
        if isin in self._threads:
            return
        t = threading.Thread(target=self._run_one, args=(isin,), daemon=True,
                             name=f"rlc-ws-{isin}")
        self._threads[isin] = t
        t.start()

    def _run_one(self, isin: str) -> None:
        import websocket  # websocket-client

        field: Optional[str] = None
        backoff = self._reconnect_min
        while not self._stop.is_set():
            ws = None
            try:
                token = self._ensure_auth()
                url = (f"wss://{self.tenant}.exirbroker.com/sle/v2/ws"
                       f"?encoding=text&authToken={token}&device=web")
                cookie = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
                # Default TLS verification ON — this WS carries the authToken; a
                # MITM must not be able to read it. The tenant serves a valid cert.
                ws = websocket.create_connection(
                    url,
                    header=[f"User-Agent: {_UA}",
                            f"Origin: https://{self.tenant}.exirbroker.com"],
                    cookie=cookie,
                    timeout=15,
                )
                ws.send("1,MW." + isin)
                logger.info("rlc_ws %s: subscribed", isin)
                backoff = self._reconnect_min
                ws.settimeout(40)
                while not self._stop.is_set():
                    frame = parse_frame(ws.recv())
                    if frame is None:
                        continue
                    if field is None:
                        rest = rlc_market.get_queue(isin) or {}
                        field = find_buy_queue_field(frame, rest.get("buy_volume"))
                        if field:
                            logger.info("rlc_ws %s: calibrated buy-queue field=%r", isin, field)
                        else:
                            continue  # can't read yet — wait for a calibratable frame
                    bv = extract_field(frame, field)
                    self.on_update(isin, bv)
            except Exception as exc:  # noqa: BLE001 — disconnect/auth → HOLD + reconnect
                logger.warning("rlc_ws %s: disconnected: %s", isin, exc)
                self.on_update(isin, None)         # fail-safe HOLD
                if "401" in str(exc):
                    try:
                        self._ensure_auth(force=True)
                    except Exception:  # noqa: BLE001 — re-auth failure must not kill the worker
                        logger.exception("rlc_ws %s: forced re-auth failed", isin)
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


__all__ = ["RlcQueueClient", "login", "find_buy_queue_field", "extract_field", "parse_frame"]
