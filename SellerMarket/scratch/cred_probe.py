#!/usr/bin/env python3
"""Read-only credential-marker probe (Step 0 of the credential-verification feature).

Goal: capture the broker's response markers that distinguish a WRONG PASSWORD
from a WRONG CAPTCHA, for ephoenix-family + ibtrader + exir. NO orders are
placed — only login attempts. Secrets/PII (token, authToken, fullName,
nationalId, …) are NEVER printed; only the diagnostic marker fields are.

Creds + OCR come from env vars (so they never enter a shell transcript):

  OCR_SERVICE_URL   e.g. http://85.133.205.190:18080            (required)
  # ephoenix / ibtrader (repeatable: provide as many as you like)
  EPH_BROKERS       comma list of ephoenix/ib broker codes, e.g. "bbi,ib"
  EPH_USER          login username (account number)
  EPH_PASS__<code>  the CORRECT password for broker <code> (per-broker)
  EPH_PASS          fallback correct password if EPH_PASS__<code> unset
  # exir (optional — skipped if EXIR_PASS unset)
  EXIR_TENANT       e.g. khobregan
  EXIR_USER         exir username
  EXIR_PASS         exir password

Run:  python scratch/cred_probe.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

import requests

TIMEOUT = 15
# Only these keys are safe to print — diagnostic, never secrets/PII.
MARKER_KEYS = (
    "message", "error", "errors", "description", "isError", "code",
    "errorCode", "title", "detail", "type", "statusCode", "status",
    "modelState", "validationErrors", "messages", "result", "succeeded",
)
SECRET_KEYS = ("token", "authToken", "rlcAuthHeader", "nt", "jwt", "accessToken")

# Reach the Iranian broker / OCR hosts DIRECTLY — never via an /etc/environment
# proxy. A foreign-exit proxy can't reach Iranian hosts and would skew the probe.
_S = requests.Session()
_S.trust_env = False


def _ocr_url() -> str:
    raw = (os.getenv("OCR_SERVICE_URL", "") or "").replace(",", " ").split()
    if not raw:
        sys.exit("OCR_SERVICE_URL not set")
    return raw[0].rstrip("/")


def solve_captcha(b64: str) -> str:
    url = f"{_ocr_url()}/ocr/captcha-easy-base64"
    r = _S.post(url, json={"base64": b64}, timeout=TIMEOUT,
                headers={"accept": "text/plain", "Content-Type": "application/json"})
    r.raise_for_status()
    s = r.text.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return s


def safe_view(status: int, body) -> dict:
    """Return a printable view: status, all keys, and only the marker fields."""
    out = {"http": status}
    if isinstance(body, dict):
        out["keys"] = sorted(body.keys())
        out["has_token"] = any(bool(body.get(k)) for k in SECRET_KEYS)
        markers = {}
        for k in MARKER_KEYS:
            if k in body:
                v = body[k]
                # `result` may be a big object on success — only note presence
                if k == "result" and isinstance(v, (dict, list)):
                    markers[k] = f"<{type(v).__name__} len={len(v)}>"
                else:
                    markers[k] = v
        out["markers"] = markers
    else:
        out["raw"] = str(body)[:300]
    return out


# ----------------------------------------------------------------------------
# ephoenix / ibtrader
# ----------------------------------------------------------------------------
def eph_endpoints(code: str) -> dict:
    is_ib = code == "ib"
    domain = "ibtrader.ir" if is_ib else "ephoenix.ir"
    prefix = "." if is_ib else f"-{code}."
    return {
        "captcha": f"https://identity{prefix}{domain}/api/Captcha/GetCaptcha",
        "login": f"https://identity{prefix}{domain}/api/v2/accounts/login",
    }


def eph_login_attempt(ep: dict, username: str, password: str,
                      *, force_wrong_captcha: bool) -> dict:
    cr = _S.get(ep["captcha"], timeout=TIMEOUT)
    cr.raise_for_status()
    cd = cr.json()
    captcha_val = "00000" if force_wrong_captcha else solve_captcha(cd["captchaByteData"])
    if not captcha_val:
        return {"http": None, "note": "ocr-empty"}
    lr = _S.post(ep["login"], timeout=TIMEOUT, json={
        "loginName": username,
        "password": password,
        "captcha": {"hash": cd["hashedCaptcha"], "salt": cd["salt"], "value": captcha_val},
    })
    try:
        body = lr.json() if lr.content else {}
    except ValueError:
        body = lr.text[:300]
    return safe_view(lr.status_code, body)


def probe_ephoenix(code: str, username: str, password: str) -> None:
    print(f"\n{'='*70}\nephoenix-family broker = {code!r}  (user masked)\n{'='*70}")
    ep = eph_endpoints(code)

    # 1) baseline: correct captcha + correct password (retry OCR misses)
    print("\n[baseline] correct captcha + correct password:")
    base_ok = None
    for i in range(6):
        try:
            v = eph_login_attempt(ep, username, password, force_wrong_captcha=False)
        except Exception as e:
            print(f"  attempt {i+1}: EXC {type(e).__name__}: {e}")
            break
        print(f"  attempt {i+1}: {json.dumps(v, ensure_ascii=False)}")
        if v.get("has_token"):
            base_ok = v
            break
        time.sleep(1)
    print(f"  -> baseline {'OK (token seen)' if base_ok else 'NOT established'}")

    # 2) wrong captcha + correct password
    print("\n[wrong-captcha] deliberately bad captcha + correct password:")
    for i in range(2):
        try:
            v = eph_login_attempt(ep, username, password, force_wrong_captcha=True)
            print(f"  attempt {i+1}: {json.dumps(v, ensure_ascii=False)}")
        except Exception as e:
            print(f"  attempt {i+1}: EXC {type(e).__name__}: {e}")
        time.sleep(1)

    # 3) wrong password + (OCR) correct captcha — run a few; the responses that
    #    differ from the wrong-captcha shape are the wrong-PASSWORD marker.
    print("\n[wrong-password] solved captcha + WRONG password:")
    for i in range(4):
        try:
            v = eph_login_attempt(ep, username, password + "_WRONG", force_wrong_captcha=False)
            print(f"  attempt {i+1}: {json.dumps(v, ensure_ascii=False)}")
        except Exception as e:
            print(f"  attempt {i+1}: EXC {type(e).__name__}: {e}")
        time.sleep(1)


# ----------------------------------------------------------------------------
# exir
# ----------------------------------------------------------------------------
def exir_login_attempt(base: str, username: str, password: str,
                       *, force_wrong_captcha: bool) -> dict:
    s = requests.Session()
    s.trust_env = False  # reach the Iranian broker host directly, not via a proxy
    s.get(base + "/exir", timeout=TIMEOUT)
    rc = s.get(base + "/captcha", timeout=TIMEOUT)
    cli = rc.headers.get("client_login_id")
    if cli:
        s.cookies.set("client_login_id", cli)
    if force_wrong_captcha:
        cap = "00000"
    else:
        cap = solve_captcha(base64.b64encode(rc.content).decode())
    if not (cap and cap.isdigit() and len(cap) == 5):
        return {"http": None, "note": f"ocr-bad ({cap!r})"}
    rl = s.post(base + "/api/v2/login", timeout=TIMEOUT,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json={"username": username, "password": password,
                      "captcha": int(cap), "otp": ""})
    try:
        body = rl.json() if rl.content else {}
    except ValueError:
        body = rl.text[:300]
    v = safe_view(rl.status_code, body)
    # exir success marker = `nt`
    if isinstance(body, dict):
        v["has_token"] = bool(body.get("nt"))
    return v


def probe_exir(tenant: str, username: str, password: str) -> None:
    base = f"https://{tenant}.exirbroker.com"
    print(f"\n{'='*70}\nexir tenant = {tenant!r}  (user masked)\n{'='*70}")
    print("\n[baseline] correct captcha + correct password:")
    base_ok = None
    for i in range(6):
        try:
            v = exir_login_attempt(base, username, password, force_wrong_captcha=False)
        except Exception as e:
            print(f"  attempt {i+1}: EXC {type(e).__name__}: {e}")
            break
        print(f"  attempt {i+1}: {json.dumps(v, ensure_ascii=False)}")
        if v.get("has_token"):
            base_ok = v
            break
        time.sleep(1)
    print(f"  -> baseline {'OK (nt seen)' if base_ok else 'NOT established'}")

    print("\n[wrong-captcha] bad captcha + correct password:")
    for i in range(2):
        try:
            v = exir_login_attempt(base, username, password, force_wrong_captcha=True)
            print(f"  attempt {i+1}: {json.dumps(v, ensure_ascii=False)}")
        except Exception as e:
            print(f"  attempt {i+1}: EXC {type(e).__name__}: {e}")
        time.sleep(1)

    print("\n[wrong-password] solved captcha + WRONG password:")
    for i in range(4):
        try:
            v = exir_login_attempt(base, username, password + "_WRONG", force_wrong_captcha=False)
            print(f"  attempt {i+1}: {json.dumps(v, ensure_ascii=False)}")
        except Exception as e:
            print(f"  attempt {i+1}: EXC {type(e).__name__}: {e}")
        time.sleep(1)


def main() -> None:
    _ocr_url()  # validate
    did = False
    user = os.getenv("EPH_USER", "")
    for code in [c.strip() for c in os.getenv("EPH_BROKERS", "").split(",") if c.strip()]:
        pw = os.getenv(f"EPH_PASS__{code}") or os.getenv("EPH_PASS")
        if not (user and pw):
            print(f"skip ephoenix {code}: missing EPH_USER / password")
            continue
        probe_ephoenix(code, user, pw)
        did = True

    if os.getenv("EXIR_PASS"):
        probe_exir(os.getenv("EXIR_TENANT", "khobregan"),
                   os.getenv("EXIR_USER", ""), os.getenv("EXIR_PASS"))
        did = True

    if not did:
        print("nothing probed — set EPH_BROKERS/EPH_USER/EPH_PASS or EXIR_*")


if __name__ == "__main__":
    main()
