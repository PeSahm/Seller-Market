"""Read-only Phase-0 spike for the Mofid / Orbis (easytrader.ir) broker family.

Confirms the live wire contract BEFORE building the adapter (mirrors the
exir/onlineplus spikes). **Places NO orders** (not even drafts — a draft is a
side effect). Creds come from env so they never hit the transcript/disk:

    MOFID_USER=... MOFID_PASS=... [MOFID_OCR=http://5.10.248.55:18080] \
        python SellerMarket/scratch/mofid_spike.py

What it checks:
  1. OAuth2 Authorization-Code + PKCE login -> access_token (+ whether the
     BotDetect captcha appears; solve via /ocr/mofid-orbis-base64).
  2. Reject markers (only triggered if creds are wrong; we don't force it).
  3. Authed reads: /core/api/money/, /core/api/portfolio/true,
     /easy/api/account/user-info, /easy/api/account/server-time/{ms},
     /core/api/order  (shapes + createDateTime format).
  4. RLC price band (core.tadbirrlc.com getstockprice2) for a held ISIN.
  5. draft/batch endpoint URLs discovered from the d.easytrader.ir SPA bundle.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import urljoin

import requests

USER = os.environ["MOFID_USER"]
PASS = os.environ["MOFID_PASS"]
OCR = os.environ.get("MOFID_OCR", "http://5.10.248.55:18080") + "/ocr/mofid-orbis-base64"

API = "https://api-mts.orbis.easytrader.ir"
OAUTH = "https://login.emofid.com"
SPA = "https://d.easytrader.ir"
REDIRECT = "https://d.easytrader.ir/auth-callback"
REFERER = "https://d.easytrader.ir/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
CLIENT_ID = "easy_pkce"
SCOPE = "easy2_api mts_api openid profile"


def _pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(72)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


_OCR_SESSION = requests.Session()
_OCR_SESSION.trust_env = False  # reach the OCR host DIRECTLY (never via a proxy)


def _solve_captcha(img_bytes):
    b64 = base64.b64encode(img_bytes).decode()
    r = _OCR_SESSION.post(OCR, json={"base64": b64},
                          headers={"accept": "text/plain", "Content-Type": "application/json"},
                          timeout=20)
    return r.text.strip().strip('"')


def _find(html, name):
    m = re.search(r'name="' + re.escape(name) + r'"[^>]*value="([^"]*)"', html)
    if not m:
        m = re.search(r'value="([^"]*)"[^>]*name="' + re.escape(name) + r'"', html)
    return m.group(1) if m else None


def login():
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA})
    verifier, challenge = _pkce()

    # Step 1 — authorize (expect 302 -> /Login?ReturnUrl=)
    q = {"client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code",
         "scope": SCOPE, "code_challenge": challenge, "code_challenge_method": "S256",
         "response_mode": "query"}
    r = s.get(OAUTH + "/connect/authorize/callback", params=q, allow_redirects=False, timeout=20)
    print(f"[1] authorize -> {r.status_code}  Location={r.headers.get('Location','')[:90]}")
    loc = r.headers.get("Location")
    if not loc:
        print("    body[:200]:", r.text[:200]); return None
    login_url = urljoin(OAUTH, loc)

    # Step 2 — GET login page (follow up to 2 hops)
    rp = s.get(login_url, allow_redirects=False, timeout=20)
    hops = 0
    while rp.status_code in (301, 302) and hops < 2:
        login_url = urljoin(login_url, rp.headers["Location"])
        rp = s.get(login_url, allow_redirects=False, timeout=20)
        hops += 1
    print(f"[2] login page -> {rp.status_code}  url={login_url[:70]}  bytes={len(rp.text)}")

    # Step 3-5 — parse form, (captcha?), POST creds, follow to token code
    for attempt in range(1, 4):
        html = rp.text
        token = _find(html, "__RequestVerificationToken")
        has_captcha = 'name="Captcha"' in html or "OLoginCaptcha_CaptchaImage" in html
        print(f"[3.{attempt}] form: verifTok={'Y' if token else 'N'} captcha={'Y' if has_captcha else 'N'} Cpr={'Y' if 'name=\"Cpr\"' in html else 'N'}")
        body = {"Username": USER, "Password": PASS,
                "__RequestVerificationToken": token or "",
                "button": "login", "RememberLogin": "false"}
        if has_captcha:
            for f in ("BDC_VCID_OLoginCaptcha", "BDC_BackWorkaround_OLoginCaptcha",
                      "BDC_Hs_OLoginCaptcha", "BDC_SP_OLoginCaptcha"):
                body[f] = _find(html, f) or ""
                print(f"        {f}={'set' if body[f] else 'MISSING'}")
            m = re.search(r'id="OLoginCaptcha_CaptchaImage"[^>]*src="([^"]+)"', html)
            if m:
                img_url = urljoin(OAUTH + "/", m.group(1).replace("&amp;", "&"))
                ir = s.get(img_url, timeout=20)
                print(f"        captcha img {ir.status_code} {ir.headers.get('Content-Type')} {len(ir.content)}B url={img_url[:70]}")
                ans = _solve_captcha(ir.content)
                body["Captcha"] = ans
                print(f"        OCR -> {ans!r} (len {len(ans)})")

        rl = s.post(login_url, data=body, allow_redirects=False, timeout=20)
        loc = rl.headers.get("Location", "")
        print(f"[4.{attempt}] login POST -> {rl.status_code}  Location={loc[:70]}  bodybytes={len(rl.text)}")
        if rl.status_code in (301, 302) and loc.startswith("/connect/authorize"):
            break  # success
        # inspect validation-summary
        vs = re.search(r'validation-summary-errors[^>]*>(.*?)</div>', rl.text, re.S)
        if vs:
            txt = re.sub(r'<[^>]+>', ' ', vs.group(1)).strip()
            print(f"        validation-summary: {txt[:160]}")
        rp = s.get(login_url, allow_redirects=False, timeout=20)  # refetch for retry
    else:
        print("    login did not reach the authorize redirect"); return None

    # Step 6 — follow /connect/authorize -> auth-callback?code=
    cont = urljoin(OAUTH, loc)
    rc = s.get(cont, allow_redirects=False, timeout=20)
    cloc = rc.headers.get("Location", "")
    print(f"[5] authorize-continue -> {rc.status_code}  Location={cloc[:80]}")
    m = re.search(r'[?&]code=([^&]+)', cloc)
    if not m:
        print("    no code"); return None
    code = m.group(1)

    # Step 7 — token exchange
    rt = s.post(OAUTH + "/connect/token",
                data={"client_id": CLIENT_ID, "code": code, "redirect_uri": REDIRECT,
                      "code_verifier": verifier, "grant_type": "authorization_code"},
                headers={"content-type": "application/x-www-form-urlencoded", "Referer": REFERER},
                allow_redirects=False, timeout=20)
    tok = rt.json() if rt.headers.get("content-type", "").startswith("application/json") else {}
    print(f"[6] token -> {rt.status_code}  keys={list(tok)}  expires_in={tok.get('expires_in')} error={tok.get('error')}")
    at = tok.get("access_token")
    if not at:
        print("    body[:200]:", rt.text[:200]); return None
    print(f"    access_token len={len(at)}  (NOT printed)")
    return at


def authed(at, method, path, **kw):
    h = {"Authorization": f"Bearer {at}", "User-Agent": UA, "Referer": REFERER,
         "Accept": "application/json, text/plain, */*"}
    s = requests.Session(); s.trust_env = False
    return s.request(method, API + path, headers=h, timeout=20, **kw)


def reads(at):
    print("\n=== AUTHED READS ===")
    # same-login (device reg) — probe but note eviction risk
    try:
        sl = authed(at, "POST", "/easy/api/account/same-login",
                    json={"uuid": "spike-readonly", "appBuildNo": "16872", "width": 1536,
                          "height": 729, "devicePlatform": "Desktop", "platformInfo": UA})
        print(f"[same-login] {sl.status_code} {sl.text[:120]}")
    except Exception as e:
        print("[same-login] ERR", e)

    ui = authed(at, "GET", "/easy/api/account/user-info")
    print(f"[user-info] {ui.status_code} keys={list(ui.json())[:12] if ui.status_code==200 else ui.text[:120]}")

    money = authed(at, "GET", "/core/api/money/")
    j = money.json() if money.status_code == 200 else {}
    print(f"[money] {money.status_code} buyPower={j.get('buyPower')} balance={j.get('balance')} keys={list(j)[:10]}")

    st = authed(at, "GET", f"/easy/api/account/server-time/{int(time.time()*1000)}")
    print(f"[server-time] {st.status_code} {st.text[:120]}")

    port = authed(at, "GET", "/core/api/portfolio/true")
    pj = port.json() if port.status_code == 200 else {}
    items = pj.get("portfolioItems") or pj.get("portfolio") or (pj if isinstance(pj, list) else [])
    print(f"[portfolio] {port.status_code} topkeys={list(pj)[:8] if isinstance(pj,dict) else 'list'} n_items={len(items) if isinstance(items,list) else '?'}")
    held_isin = None
    if isinstance(items, list) and items:
        print("   item[0] keys:", list(items[0]))
        print("   item[0]:", json.dumps(items[0], ensure_ascii=False)[:200])
        held_isin = items[0].get("isin") or items[0].get("symbolIsin")

    orders = authed(at, "GET", "/core/api/order")
    oj = orders.json() if orders.status_code == 200 else {}
    rows = oj.get("orders") if isinstance(oj, dict) else (oj if isinstance(oj, list) else [])
    print(f"[orders] {orders.status_code} topkeys={list(oj)[:8] if isinstance(oj,dict) else 'list'} n={len(rows) if isinstance(rows,list) else '?'}")
    if isinstance(rows, list) and rows:
        print("   order[0] keys:", list(rows[0]))
        print("   order[0]:", json.dumps(rows[0], ensure_ascii=False)[:300])
    return held_isin


def rlc_band(isin):
    isin = isin or "IRO1MSMI0001"  # فملی blue-chip if account holds nothing
    print(f"\n=== RLC price band for {isin} ===")
    s = requests.Session(); s.trust_env = False
    q = json.dumps({"Type": "getstockprice2", "la": "Fa", "arr": isin})
    url = f"https://core.tadbirrlc.com//StockInformationHandler?{q}&jsoncallback="
    r = s.get(url, headers={"User-Agent": UA}, timeout=20)
    print(f"   {r.status_code} {r.text[:400]}")


def discover(at=None):
    print("\n=== draft/batch URL discovery (d.easytrader.ir SPA bundle) ===")
    s = requests.Session(); s.trust_env = False
    s.headers.update({"User-Agent": UA})
    root = s.get(SPA + "/", timeout=20)
    bundles = re.findall(r'(?:src|href)="([^"]+\.js)"', root.text)
    print(f"   index.html {root.status_code}, js refs: {bundles}")
    api_paths, ctx = set(), []
    for b in bundles:
        try:
            jr = s.get(urljoin(SPA + "/", b), timeout=30)
        except Exception as e:
            print("   fetch fail", b, e); continue
        txt = jr.text
        for m in re.findall(r'/?(?:core|easy)/api/[A-Za-z0-9/_.{}$-]+', txt):
            api_paths.add(m if m.startswith("/") else "/" + m)
        for kw in ("draft", "batch"):
            for mm in re.finditer(kw, txt, re.I):
                a, z = max(0, mm.start() - 45), mm.end() + 35
                ctx.append(txt[a:z].replace("\n", " "))
    print(f"   --- {len(api_paths)} api paths ---")
    for p in sorted(api_paths):
        flag = " <== DRAFT/BATCH" if re.search(r'draft|batch', p, re.I) else ""
        print(f"   {p}{flag}")
    print(f"   --- draft/batch context snippets ({len(ctx)}) ---")
    for c in sorted(set(ctx))[:30]:
        print("   …", c)

    if at:
        print("   --- read-only GET-probes of candidate draft endpoints ---")
        for p in ("/core/api/draft", "/core/api/drafts", "/core/api/v2/draft",
                  "/core/api/order/draft", "/core/api/v2/order/draft",
                  "/core/api/draftorder", "/core/api/v2/order"):
            try:
                rr = authed(at, "GET", p)
                print(f"   GET {p} -> {rr.status_code} {rr.text[:80]}")
            except Exception as e:
                print(f"   GET {p} -> ERR {e}")


if __name__ == "__main__":
    print("=== Mofid/Orbis Phase-0 spike (READ-ONLY, no orders) ===")
    at = login()
    isin = reads(at) if at else None
    rlc_band(isin)
    discover(at)
    print("\n=== done ===")
