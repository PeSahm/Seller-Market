"""
THROWAWAY read-only validation spike for the Exir / Rayan HamAfza broker family.

Purpose (Phase 0 gate of the Exir feature): confirm the LIVE wire shape of a
real exirbroker.com tenant before we commit the adapter design. It logs in and
reads buyingPower + orderbookReport ONLY. **It NEVER places an order** — the
order endpoint (/api/v1/order) is intentionally not referenced anywhere in this
file.

Run from the SellerMarket package dir so `captcha_utils` imports cleanly:

    cd SellerMarket
    OCR_SERVICE_URL=http://5.10.248.55:18080 python scratch/exir_spike.py

(Windows PowerShell: `$env:OCR_SERVICE_URL="http://5.10.248.55:18080"; python scratch/exir_spike.py`)

What it pins down (the spike acceptance gate):
  1. OCR decodes the 5-digit NUMERIC captcha PNG (the OCR model was tuned for the
     ephoenix hashed-captcha — this is the single biggest risk).
  2. The live login request/response shape + where `nt`, `authToken`,
     `bourseAccountName`, and the numeric brokerCode/account id live.
  3. Whether the ported X-App-N token is accepted (200 vs 401), and which
     time-basis (UTC vs local Tehran) the server expects.
  4. The REAL orderbookReport / portfoReport row field names, and whether
     `mmtpOrderId` is numeric + unique (it becomes our dedup key).

This file is disposable — it lives under scratch/ and is not imported by the app.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# The live responses contain Persian (Farsi) text; force UTF-8 stdout so the
# Windows cp1252 console doesn't choke when we print them.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Reuse the existing OCR helper (POST base64 image -> decoded text).
from captcha_utils import decode_captcha, OCR_SERVICE_URL

# --- target (read from env so no credential is committed) --------------------
#   EXIR_TENANT (default "khobregan"), EXIR_USERNAME, EXIR_PASSWORD
#   e.g. (PowerShell):  $env:EXIR_USERNAME="..."; $env:EXIR_PASSWORD="..."; python scratch/exir_spike.py
TENANT = os.environ.get("EXIR_TENANT", "khobregan")
BASE = f"https://{TENANT}.exirbroker.com"
USERNAME = os.environ.get("EXIR_USERNAME", "")
PASSWORD = os.environ.get("EXIR_PASSWORD", "")

CAPTCHA_RETRIES = 6
TIMEOUT = 20
TEHRAN = timezone(timedelta(hours=3, minutes=30))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def hr(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def build_app_n(nt: str, path_with_query: str, now) -> str:
    """Port of ExirTokenCrypto.BuildAppNToken.

    `path_with_query` is the EXACT path (incl. query string) as it appears after
    the host — the C# passes the full `text3` (path+query) into BuildHeaders for
    GETs that have a query, and the bare path for the others. `now` selects the
    time basis (UTC in the C#; we also try Tehran as a fallback).
    """
    text = nt[2:]
    if len(text) - 5 <= 0:
        raise ValueError(f"nt too short for token algo: len(nt)={len(nt)!r}")
    char_sum = sum(ord(c) for c in path_with_query)
    t = 3600 * now.hour + 60 * now.minute + now.second
    idx = abs(t % (len(text) - 5) - int(nt[0:2]))
    return f"{int(text[idx:idx + 5]) * t * char_sum}.{t * char_sum}"


def signed_get(session, nt, path_with_query, *, prefer="utc"):
    """GET {BASE}{path_with_query} with an X-App-N header.

    Tries the preferred time basis first; on a 401/403 flips UTC<->Tehran and
    retries once, reporting which basis the server accepted. Returns
    (response, basis_used).
    """
    bases = ["utc", "tehran"] if prefer == "utc" else ["tehran", "utc"]
    last = None
    for basis in bases:
        now = datetime.now(timezone.utc) if basis == "utc" else datetime.now(TEHRAN)
        token = build_app_n(nt, path_with_query, now)
        headers = {"X-App-N": token, "Accept": "application/json"}
        resp = session.get(BASE + path_with_query, headers=headers, timeout=TIMEOUT)
        print(f"  [{basis}] X-App-N={token!r} -> HTTP {resp.status_code}")
        last = (resp, basis)
        if resp.status_code not in (401, 403):
            return resp, basis
    return last


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Standard JDF Gregorian->Jalali conversion."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    )
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


def jalali_str(dt: datetime) -> str:
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    return f"{jy:04d}/{jm:02d}/{jd:02d}"


def main() -> int:
    if not USERNAME or not PASSWORD:
        print("Set EXIR_USERNAME and EXIR_PASSWORD env vars (and optionally EXIR_TENANT).")
        return 1
    print(f"OCR_SERVICE_URL = {OCR_SERVICE_URL}")
    print(f"BASE            = {BASE}")
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})

    # --- Step 1: bootstrap cookies ------------------------------------------
    hr("STEP 1  GET /exir  (cookie bootstrap)")
    r1 = s.get(BASE + "/exir", timeout=TIMEOUT)
    print(f"HTTP {r1.status_code}")
    print("cookies:", s.cookies.get_dict())

    # --- Step 2: captcha + OCR (retry loop) ---------------------------------
    hr("STEP 2  GET /captcha  ->  OCR")
    captcha_text = ""
    for attempt in range(1, CAPTCHA_RETRIES + 1):
        rc = s.get(BASE + "/captcha", timeout=TIMEOUT)
        if attempt == 1:
            print("captcha response headers:", dict(rc.headers))
        client_login_id = rc.headers.get("client_login_id")
        if client_login_id:
            s.cookies.set("client_login_id", client_login_id)
        b64 = base64.b64encode(rc.content).decode()
        captcha_text = decode_captcha(b64)
        print(
            f"  attempt {attempt}: status={rc.status_code} bytes={len(rc.content)} "
            f"client_login_id={client_login_id!r} ocr={captcha_text!r}"
        )
        if captcha_text and captcha_text.isdigit() and len(captcha_text) == 5:
            break
    if not (captcha_text and captcha_text.isdigit()):
        print("!! OCR did not return a numeric captcha — spike gate FAILS here.")
        return 2

    # --- Step 3: login ------------------------------------------------------
    hr("STEP 3  POST /api/v2/login")
    # C# inserts the captcha raw (unquoted) -> it is a JSON NUMBER. Mirror that.
    login_body = {
        "username": USERNAME,
        "password": PASSWORD,
        "captcha": int(captcha_text),
        "otp": "",
    }
    print("request body:", json.dumps({**login_body, "password": "***"}))
    rl = s.post(
        BASE + "/api/v2/login",
        json=login_body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    print(f"HTTP {rl.status_code}")
    try:
        login_json = rl.json()
    except Exception:
        print("non-JSON login response:", rl.text[:2000])
        return 3
    # Redact nothing except password (we need to SEE nt/authToken shape).
    print("response JSON:", json.dumps(login_json, ensure_ascii=False, indent=2)[:4000])
    print("cookies after login:", s.cookies.get_dict())

    nt = login_json.get("nt")
    auth_token = login_json.get("authToken")
    if not nt:
        print("!! no `nt` in login response — cannot compute X-App-N. Gate FAILS.")
        return 3
    print(f"\nnt={nt!r}  (len={len(nt)})")
    print(f"authToken present={bool(auth_token)}  bourseAccountName={login_json.get('bourseAccountName')!r}")
    print(f"validity={login_json.get('validity')!r}")
    print(">> Inspect the JSON above for the numeric brokerCode/account id (Phase-2 order payload needs it).")

    # --- Step 4 & 5: buyingPower with X-App-N -------------------------------
    hr("STEP 4/5  GET /api/v2/user/buyingPower  (X-App-N)")
    bp_path = "/api/v2/user/buyingPower"
    rbp, basis = signed_get(s, nt, bp_path, prefer="utc")
    print(f"=> accepted basis: {basis if rbp.status_code == 200 else 'NONE (still 401/403)'}")
    print(f"HTTP {rbp.status_code}")
    print("body:", rbp.text[:1500])
    if rbp.status_code != 200:
        print("!! buyingPower not 200 — X-App-N likely wrong (time basis / path). Gate is INCOMPLETE.")

    # --- Step 6: orderbookReport (status 2 active, then 3 filled) ------------
    today = datetime.now(TEHRAN)
    start = today - timedelta(days=30)
    j_start, j_end = jalali_str(start), jalali_str(today)
    for status_id, label, dates in (
        (2, "ACTIVE", ("", "")),
        (3, "FILLED", (j_start, j_end)),
    ):
        hr(f"STEP 6  GET /api/v1/user/orderbookReport  orderStatusId={status_id} ({label})")
        sd, ed = dates
        path = (
            f"/api/v1/user/orderbookReport?size=1000&startDate={sd}"
            f"&mmtpTypeId=null&endDate={ed}&orderStatusId={status_id}"
        )
        # X-App-N is computed over the FULL path+query (matches C# BuildHeaders(text3)).
        rob, basis = signed_get(s, nt, path, prefer=basis if rbp.status_code == 200 else "utc")
        print(f"HTTP {rob.status_code} (basis={basis})")
        try:
            ob = rob.json()
            rows = ob.get("result") or []
            print(f"result rows: {len(rows)}")
            if rows:
                print("first row keys:", list(rows[0].keys()))
                print("first row:", json.dumps(rows[0], ensure_ascii=False, indent=2)[:2500])
                ids = [r.get("mmtpOrderId") for r in rows]
                print(f"mmtpOrderId sample: {ids[:5]}  unique={len(set(ids)) == len(ids)}")
        except Exception:
            print("non-JSON orderbook response:", rob.text[:1500])

    hr("DONE — no orders were placed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
