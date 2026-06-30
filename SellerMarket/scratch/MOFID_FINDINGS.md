# Mofid / Orbis (easytrader.ir) — Phase-0 spike findings (LIVE-CONFIRMED)

Read-only spike against the operator's own account (NO orders placed). Reusable
spike: `mofid_spike.py` (env creds `MOFID_USER`/`MOFID_PASS`, optional
`MOFID_OCR`). Family code = **`mofid`**. OCR route = **`/ocr/mofid-orbis-base64`**.

## Hosts
- API (MTS): `https://api-mts.orbis.easytrader.ir/` (`mtsPath`; stage = `https://stage-api-mts.easytrader.ir/`). Single host, no tenant.
- OAuth (IdentityServer): `https://login.emofid.com`
- SPA: `https://d.easytrader.ir` (Angular, one bundle `main-*.js`)
- Headers: `Referer: https://d.easytrader.ir/`, UA `…Chrome/131.0.0.0 Safari/537.36`, auth `Authorization: Bearer <access_token>`.

## OAuth2 Authorization-Code + PKCE — CONFIRMED WORKING (first attempt, NO captcha)
8 steps, manual redirects (`allow_redirects=False`), `requests.Session` carries the cookie jar:
1. PKCE: `code_verifier` (96 url-safe chars), `code_challenge = base64url(sha256(verifier))` no padding.
2. `GET login.emofid.com/connect/authorize/callback?client_id=easy_pkce&redirect_uri=https://d.easytrader.ir/auth-callback&response_type=code&scope=easy2_api mts_api openid profile&code_challenge=…&code_challenge_method=S256&response_mode=query` → **302** `Location: /Login?ReturnUrl=…`
3. `GET` that `/Login?ReturnUrl=…` → 200 HTML (~26 KB). Cookies set on the session.
4. Parse `__RequestVerificationToken` (+ optional captcha block). **First attempt: NO captcha** (matches operator).
5. `POST` the same `/Login?ReturnUrl=…` `application/x-www-form-urlencoded`: `Username, Password, __RequestVerificationToken, button=login, RememberLogin=false` → success = **empty body + 302** `Location: /connect/authorize/callback?...`
6. `GET login.emofid.com{Location}` → **302** `Location: https://d.easytrader.ir/auth-callback?code=<CODE>`
7. `POST login.emofid.com/connect/token` urlencoded `client_id=easy_pkce, code, redirect_uri, code_verifier, grant_type=authorization_code` → `{id_token, access_token, expires_in:43200, token_type, scope}`. **Token TTL = 43200s (12h).**
8. `POST api-mts…/easy/api/account/same-login` JSON `{uuid, appBuildNo:"16872", width:1536, height:729, devicePlatform:"Desktop", platformInfo:<UA>}` → 200 (empty). (Device reg; see same-login note.)

Captcha (if it appears on a retry): `<img id="OLoginCaptcha_CaptchaImage" src=…>` on `login.emofid.com`, plus hidden `BDC_VCID_OLoginCaptcha / BDC_BackWorkaround_OLoginCaptcha / BDC_Hs_OLoginCaptcha / BDC_SP_OLoginCaptcha`; download with the page cookie → `decode_captcha(b64, ocr_path="/ocr/mofid-orbis-base64")` → append `Captcha=<answer>` + the 4 BDC_* fields. (Not triggered this spike — account wasn't rate-limited.)

Reject markers (HTML `<div class="validation-summary-errors">`): wrong creds `نام کاربری یا کلمه عبور نادرست است`; captcha required `کد امنیتی را وارد کنید.`; wrong captcha `کد امنیتی اشتباه است`; token `{error:"invalid_grant"}`. (Not triggered — creds were correct; classifier built conservatively from the decompiled.)

## Authed reads — CONFIRMED (Bearer + Referer + UA)
- `GET /easy/api/account/user-info` → `{name, family, gender, bourseCode, mobile, email}` (account name = `name`+`family`).
- `GET /core/api/money/` → `{balance, buyPower0, buyPower1, buyPower, blocked, credit, avandCredit}` — use **`buyPower`** (live: 31,224,500).
- `GET /easy/api/account/server-time/{local_ms}` → `{diff, serverTimestamp}` (diff ≈ 1.5 s).
- `GET /core/api/portfolio/true` → `{portfolioItems:[{isin, asset, …}]}` (asset = qty; keep >0). _(Account held 0 this spike.)_
- `GET /core/api/order` → `{orders:[…], algoSuffixCharacters}`. _(0 orders this spike → `createDateTime` format UNCONFIRMED; `_map_mofid_row` handles ISO-Gregorian and Jalali.)_

## ORDER FIRING — draft + batch (the SPA's mechanism; matches Orbis.py)
Extracted from the SPA bundle (`draftService` `Ue$1`, `orderUrl = mtsPath+apiUrls.oms` = `…/core/api/`, `draftUrl = mtsPath+apiUrls.easy+"draft"` = `…/easy/api/draft`):
- **Draft create**: `POST {API}/easy/api/draft` body `{"draft":{symbolIsin, symbolName, price, quantity, side, validityType, validityDate}}` → `{id}` (success ⇔ `id` present). Repeat N× → N draft ids. _(GET /easy/api/draft is perm-gated 403; POST create is the firing path — confirm at canary.)_
- **Batch create (fires the orders)**: `POST {API}/core/api/order/batchCreate` body `{"draftIds":[ids], "removeDraftAfterCreate":false, "orderFrom":34}`.
- **Single immediate order** (auto-sell SELL): `POST {API}/core/api/v2/order` body `{"order":{orderFrom:34, price:"str", quantity:"str", side, symbolIsin, validityType:0}}` → `isSuccessful==true`; `omsError[].code==8706` = market closed.
- Other: `POST /core/api/order/batchDelete`, `DELETE /core/api/v2/delete-order`, `GET /core/api/v2/order/today-activities` (403 perm-gated).

**Side encoding: `Buy=0, Sell=1`** (bundle `[e.Buy=0]="Buy"` + decompiled `OrderSideMofid`). Bot config side (1=buy/2=sell) → Mofid side: **1→0, 2→1**. validityType `Day=0`.

**N drafts rationale + safety**: each draft is the FULL-volume order; a batch of N identical drafts → at most 1 fills (the rest rejected for insufficient buying power), so **N>1 cannot over-buy**. Orbis.py used N=10 for queue-race redundancy. Default the bot to a SAFE small N (config `mofid_draft_count`, default 1 for the canary) — note that with N=1 this is ~equivalent to the single `v2/order` plus one batch hop; bump N to restore Orbis.py's redundancy.

**Server-time window**: `GET /easy/api/account/server-time/{local_ms}` → `diff`; align the batch-fire window to the broker clock (Orbis.py used 08:44:58.450–08:45:00.900 server time).

## Price band + fee
- **Price band: REUSE `rlc_price`** — `core.tadbirrlc.com//StockInformationHandler?getstockprice2&arr=<ISIN>` covers Mofid instruments. Live فملی `IRO1MSMI0001` → `hap=20930` (BUY ceiling), `lap=19730` (SELL floor), `mxqo=100000` (max order qty), `bbq` (buy-queue). ISIN-keyed, public, no auth.
- **Fee: no live Mofid wages endpoint** (the SPA path enumeration shows no `wage`/`commission` API; the decompiled `CommissionService` is file-based `Commissions.json`). **Use the ~0.005 fallback** for `floor(BP/(price*(1+fee)))`. Confirm against a real fill at canary.

## 1500 requests/hour cap
Server-side, per account. One OAuth login ≈ 6 requests (+ same-login + optional captcha img). Reads are cheap. The bounded firer (stop-on-first-success + hard `mofid_max_fire_attempts` cap) + the 12h token cache keep a run far under the cap.

## Open items (confirm at canary / low-risk)
- `createDateTime` format (no orders this spike) — mapper handles ISO + Jalali.
- `same-login` eviction semantics (single session this spike; mgmt verify will NOT call same-login to be safe — it's read-only).
- Draft POST + batchCreate live success shape (GETs were perm-gated 403; the POST firing path is exercised at the canary).
- Per-order rate gate vs only-1500/hr (not probed; the firer is bounded regardless).
- mgmt→Mofid host reachability from PouyanIt/ParsPack (spike ran from the Windows host OK; the resilient verify-proxy covers any per-network block).
