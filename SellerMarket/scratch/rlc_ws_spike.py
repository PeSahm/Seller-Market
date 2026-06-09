"""THROWAWAY read-only spike: pin the RLC/Exir market-data WebSocket buy-queue field.

Phase-0b gate for issue #110 (auto-sell). The Exir SPA streams live market data
over a plain JSON WebSocket (NOT Lightstreamer — see RLC_WS_FINDINGS.md):

    wss://<tenant>.exirbroker.com/sle/v2/ws?encoding=text&authToken=<authToken>&device=web

Subscribe to one instrument's Market-Watch channel with the text frame
``"1,MW.<ISIN>"`` and the server pushes JSON frames (``{"msgType": ...}``). This
spike logs in with the Khobregan account (reusing the captcha->OCR->login flow),
subscribes, prints the push frames, and looks for the field whose value equals the
REST best-buy-queue (``rlc_market.get_queue(isin)['buy_volume']`` == ``bbq``). It
also opens two sockets to test whether one account allows concurrent sessions.

**It NEVER places an order** — ``/api/v1/order`` is not referenced anywhere here.

Needs ``websocket-client`` (``pip install websocket-client``). Run from SellerMarket:

    cd SellerMarket
    OCR_SERVICE_URL=http://5.10.248.55:18080 EXIR_USERNAME=... EXIR_PASSWORD=... \
        python scratch/rlc_ws_spike.py

(PowerShell: set the env vars with $env:NAME="..." first.)
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import threading
import time

import requests

# Make the flat top-level modules (captcha_utils, rlc_market) importable when this
# is launched as ``python scratch/rlc_ws_spike.py`` (script dir is scratch/, so add
# its parent — the SellerMarket package root — to the path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from captcha_utils import decode_captcha, OCR_SERVICE_URL  # noqa: E402
import rlc_market  # noqa: E402  (REST cross-check: buy_volume == bbq)

TENANT = os.environ.get("EXIR_TENANT", "khobregan")
BASE = f"https://{TENANT}.exirbroker.com"
WS_URL_TMPL = f"wss://{TENANT}.exirbroker.com/sle/v2/ws?encoding=text&authToken={{token}}&device=web"
USERNAME = os.environ.get("EXIR_USERNAME", "")
PASSWORD = os.environ.get("EXIR_PASSWORD", "")
ISIN = os.environ.get("RLC_WS_ISIN", "IRO1SROD0001")  # سرود — known-live instrument
CAPTCHA_RETRIES = 6
TIMEOUT = 20
LISTEN_SECONDS = 30
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def hr(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def login() -> tuple[requests.Session, str]:
    """captcha -> OCR -> POST /api/v2/login. Return (session, authToken)."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    s.get(BASE + "/exir", timeout=TIMEOUT)  # cookie bootstrap

    captcha_text = ""
    for attempt in range(1, CAPTCHA_RETRIES + 1):
        rc = s.get(BASE + "/captcha", timeout=TIMEOUT)
        cli = rc.headers.get("client_login_id")
        if cli:
            s.cookies.set("client_login_id", cli)
        captcha_text = decode_captcha(base64.b64encode(rc.content).decode())
        print(f"  captcha attempt {attempt}: bytes={len(rc.content)} ocr={captcha_text!r}")
        if captcha_text and captcha_text.isdigit() and len(captcha_text) == 5:
            break
    if not (captcha_text and captcha_text.isdigit()):
        raise SystemExit("!! OCR did not return a numeric captcha — gate FAILS here.")

    rl = s.post(
        BASE + "/api/v2/login",
        json={"username": USERNAME, "password": PASSWORD, "captcha": int(captcha_text), "otp": ""},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    body = rl.json()
    token = body.get("authToken")
    if not token:
        raise SystemExit(f"!! login failed (HTTP {rl.status_code}); keys={sorted(body.keys())}")
    # authToken/cookies are reusable secrets — presence/length only, never the value.
    print(f"  login ok: authToken present (len={len(token)}); cookies={list(s.cookies.keys())}")
    return s, token


def _cookie_header(s: requests.Session) -> str:
    return "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())


def run_socket(token: str, cookies: str, label: str, frames: list) -> None:
    """Open one WS, subscribe MW.<ISIN>, collect frames for LISTEN_SECONDS."""
    import websocket  # websocket-client

    url = WS_URL_TMPL.format(token=token)
    ws = websocket.create_connection(
        url,
        header=[f"User-Agent: {UA}", f"Origin: {BASE}"],
        cookie=cookies,
        sslopt={"cert_reqs": ssl.CERT_NONE},
        timeout=15,
    )
    print(f"[{label}] connected")
    sub = "1,MW." + ISIN
    ws.send(sub)
    print(f"[{label}] sent subscribe: {sub!r}")
    ws.settimeout(2)
    end = time.time() + LISTEN_SECONDS
    while time.time() < end:
        try:
            msg = ws.recv()
        except Exception:
            continue
        if not msg:
            continue
        frames.append(msg)
        try:
            obj = json.loads(msg)
            print(f"[{label}] msgType={obj.get('msgType')!r} keys={sorted(obj.keys())[:24]}")
        except Exception:
            print(f"[{label}] non-JSON frame: {msg[:200]!r}")
    try:
        ws.close()
    except Exception:
        pass
    print(f"[{label}] closed; frames={len(frames)}")


def find_buy_queue_field(frames: list, rest_bbq) -> None:
    hr(f"MATCH the MW-frame field to REST buy_volume (bbq={rest_bbq!r})")
    seen_market_frame = False
    for raw in frames:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if obj.get("msgType") in ("connect", "time", None):
            continue
        seen_market_frame = True
        print("MW frame:", json.dumps(obj, ensure_ascii=False)[:1800])
        for k, v in obj.items():
            if isinstance(v, (int, float)) and rest_bbq and v == rest_bbq:
                print(f"  >>> candidate buy-queue field: {k!r} == {v}")
        # nested dicts (e.g. an orderbook/queue sub-object)
        for k, v in obj.items():
            if isinstance(v, dict):
                for kk, vv in v.items():
                    if isinstance(vv, (int, float)) and rest_bbq and vv == rest_bbq:
                        print(f"  >>> candidate nested buy-queue field: {k}.{kk} == {vv}")
    if not seen_market_frame:
        print("  (no non-connect/time frames captured — market may be closed; re-run in-session)")


def main() -> int:
    if not USERNAME or not PASSWORD:
        print("Set EXIR_USERNAME / EXIR_PASSWORD (and OCR_SERVICE_URL).")
        return 1
    print(f"OCR_SERVICE_URL={OCR_SERVICE_URL}  BASE={BASE}  ISIN={ISIN}")

    hr("REST cross-check  rlc_market.get_queue(ISIN)")
    rest = rlc_market.get_queue(ISIN)
    print("REST queue:", rest)
    rest_bbq = (rest or {}).get("buy_volume")

    hr("LOGIN (captcha -> OCR -> /api/v2/login)")
    s, token = login()
    cookies = _cookie_header(s)

    hr(f"WS  subscribe MW.{ISIN}  (listen {LISTEN_SECONDS}s)")
    frames: list = []
    run_socket(token, cookies, "A", frames)
    find_buy_queue_field(frames, rest_bbq)

    hr("CONCURRENT-SESSION test (two sockets, one account)")
    fa, fb = [], []
    ta = threading.Thread(target=run_socket, args=(token, cookies, "C1", fa))
    tb = threading.Thread(target=run_socket, args=(token, cookies, "C2", fb))
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    print(f"concurrent: C1 frames={len(fa)}  C2 frames={len(fb)}  "
          f"(both > 0 => one account allows concurrent WS sessions)")

    hr("DONE — no orders were placed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
