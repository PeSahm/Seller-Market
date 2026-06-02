# Exir / Rayan HamAfza — LIVE wire shape (confirmed by Phase-0 spike, 2026-06-02)

Tenant probed: `khobregan` → `https://khobregan.exirbroker.com`. Account 4580090306.
Source of truth for the adapter. (Spike: `scratch/exir_spike.py`, read-only, no orders.)

## Auth (web platform = cookies + X-App-N)
1. `GET /exir` → sets cookie `cookiesession1`.
2. `GET /captcha` → **JPEG** image (~3 KB), `Content-Type: image/jpeg`; response header
   **`client_login_id`** = a JWT, also `Set-Cookie: client_login_id=...; Max-Age=120`.
   The existing OCR service decodes it fine → **5 numeric digits** (e.g. `78529`).
3. `POST /api/v2/login` JSON body `{"username","password","captcha":<int>,"otp":""}`
   — **captcha is a JSON NUMBER, not a string**. → HTTP 200.
   - Response (real keys): `username` ("116"+account), `firstName`/`lastName` (Persian),
     `authToken` (JWT), **`nt`** (130-char numeric seed for X-App-N), `validity` (minutes, 480),
     `accountNumberList[0].bourseAccountName` (e.g. "اسمـ50113") + `.accountNumber`
     ("11694580090306" — **ends with the username**), `bankAccounts[0].id == -1`
     (matches the order payload's `bankAccountId:-1`), `brokerName` ("خبرگان سهام;khobregan saham"),
     `sendOrderDelay` (400). **No top-level `bourseAccountName`** — read it from `accountNumberList[0]`.
   - Auth state after login = session cookies (`JWT-TOKEN` = authToken, `cookiesession1`,
     `client_login_id`). No `Authorization: Bearer` header is needed for reads.
   - **Broker numeric id = 116** (from the `authToken`/`rlcAuthHeader` JWT `"b":116` claim;
     username/account are prefixed with "116"). Needed for the Phase-2 order payload `brokerCode`.

## X-App-N (per-request signature) — CONFIRMED
- `X-App-N = build_app_n(nt, path_with_query)` where, **proven against the live 200**:
  - **time basis = UTC** (`datetime.utcnow()`), and
  - **the signed string is the FULL path INCLUDING the query string** (e.g.
    `/api/v1/user/orderbookReport?size=1000&startDate=...&orderStatusId=2`), matching the C#
    `BuildHeaders(text3)`.
- Algorithm:
  ```python
  text = nt[2:]; char_sum = sum(ord(c) for c in path_with_query)
  t = 3600*utc.hour + 60*utc.minute + utc.second
  idx = abs(t % (len(text)-5) - int(nt[0:2]))
  token = f"{int(text[idx:idx+5]) * t * char_sum}.{t * char_sum}"
  ```
- Recompute immediately before each request (changes every second). `len(nt)`≈130 so `len(text)-5`>0.

## Reads
- **orderbookReport** (CONFIRMED 200): `GET /api/v1/user/orderbookReport?size=1000&startDate={J}&mmtpTypeId=null&endDate={J}&orderStatusId={2|3|4}`
  - `{J}` = **Jalali** `YYYY/MM/DD` (empty allowed → all). Status: 2=active, 3=filled, 4=cancelled.
  - Response `{"result":[ row, ... ]}`. **Row fields (live):**
    `mmtpOrderId` (int, unique → **tracking_number** dedup key), `uuid` (str),
    `orderSideName` = **"خريد"** (buy, note Arabic ي) / **"فروش"** (sell) — match on first letter
    (خ=buy / ف=sell) to be robust to ي/ی spelling, `quantity`, `remainingQuantity`,
    `tradedQuantity`, `price`, `averageTradedPrice`, `totalValue`, `pureValue`,
    **`insMaxLCode`** = the **ISIN** (e.g. "IRO1SROD0001") → maps to our `isin` column directly,
    `farsiName` (instrument title, e.g. "سيمان شاهرود") → `symbol_title`,
    `mmtpOrderStatusName` (e.g. "در صف"=in-queue) → `state_desc`,
    `entryDateTime` = **Jalali** "YYYY/MM/DD-HH:mm:ss" (e.g. "1405/03/12-13:27:08") → parse to `placed_at`,
    `accountNumber` (ends with username) → `pam_code` equivalent, `validityTypeName`, `bourseAccount`,
    `customerBourseAccount`.
  - **Implication:** `insMaxLCode` is an ISIN → the ephoenix `isin`-keyed schema fits with NO hack
    and NO migration. Map `state` = 3 for filled rows so `profit_report`'s `state==3` filter passes.

## Open for Phase 2 (not needed for Phase-1 reads/report)
- **buyingPower**: `GET /api/v2/user/buyingPower` returned **HTTP 406** errorCode 4047
  "service is not acceptable" — a BUSINESS error (the token was accepted; orderbook proved it),
  i.e. wrong path/version for this broker. Find the correct buyingPower path before Phase-2 BUY sizing
  (candidates: `/api/v3/user/livePortfoReport`, a different buyingPower version, or carry price in config).
- Order placement: `POST /api/v1/order` (decompiled) — confirm `insMaxLcode`=ISIN there too, and the
  `brokerCode:116` numeric id; response has no order id (ids via `wss://…/sle`). NOT exercised by the spike.
