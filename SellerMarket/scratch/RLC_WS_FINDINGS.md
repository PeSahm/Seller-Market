# RLC / Exir market-data WebSocket — Phase-0 findings (#110 auto-sell)

Read-only reverse-engineering of the Exir/RLC live market-data stream, from the
Angular SPA bundle of `https://khobregan.exirbroker.com/exir/` (reachable 200 from
the Iranian host). **No orders, no live WS connect yet** — bundle analysis only.

> ## ⚠️ CORRECTION — confirmed LIVE on PouyanIt (2026-06, Khobregan login)
> The bundle-only derivation below got three things WRONG; the live connect fixed them.
> `rlc_ws.py` now uses these (the calibration also confirmed live):
> - **WS host = one of the login response's `pushServerUrls`** (e.g.
>   `push103.irbroker.com`, `push3.rhabroker.ir`), **NOT** the tenant host.
>   `wss://<pushServerUrl>/v2/ws?encoding=text&authToken=<rlcAuthHeader>&device=web`.
>   (The bundle's `assignWsUrl()` reads `userInfo.pushServerUrls`; `baseWsSleUrl:"/sle"`
>   is the REST base, not the WS path.)
> - **Auth param = the login response's `rlcAuthHeader`** (the JWT with the broker
>   claim), **NOT** the login `authToken`.
> - **Inbound frames are comma-separated TEXT, NOT JSON.** The MW frame is
>   `MW,<insCode>,<ISIN>,<name>,<price>,<lap>,<hap>,...` — ~85 positional fields; the
>   queue numbers are positional. A `V,,,<date>,<n>` frame carries server time.
>   (The SPA's `parseMessage` JSON.parse is a different code path.)
> - The exact buy-queue field INDEX is **self-calibrated** at runtime by matching a
>   field's value to the REST `bbq` (`rlc_market.get_queue`) — it needs LIVE data
>   (at market close every queue field is 0 → ambiguous → it waits). Subscribe is
>   still `"1,MW.<ISIN>"`. Login (captcha→OCR→login), the published port, and the
>   upstream WS connect are all verified working market-closed.

## Headline: it is NOT Lightstreamer

CLAUDE.md assumed the queue streams over **Lightstreamer** (`push*.rhbroker.ir`).
**Wrong.** No `lightstreamer` / `rhbroker` / `tadbirrlc` / adapter-set string
appears in ANY bundle (`main`, `vendor`, `scripts`). The Exir SPA uses a plain
**raw WebSocket** with **JSON frames** and a tiny custom text subscribe protocol.
This massively simplifies the build — a `websockets` client, no LS library.

## The connection (confirmed from `main-es2015.*.js`)

- **URL**: `` `${this.url}/v2/ws?encoding=text&authToken=${clientId}&device=web` ``
  where the base path is `baseWsSleUrl:"/sle"`. So:
  ```
  wss://khobregan.exirbroker.com/sle/v2/ws?encoding=text&authToken=<AUTHTOKEN>&device=web
  ```
- **Auth = the Exir `authToken`** — the SAME token `exir_adapter` already gets from
  the login flow (`POST /api/v2/login` → `authToken`). **No new auth scheme.** It's
  passed as a query param (`authToken=`), not a header.
- Implemented client-side as a `ReconnectingWebSocket` (its log line: `connecting => rlc`).

## The protocol (custom text out, JSON in)

**Outbound** (what the client `.send()`s) — `"<opcode>,<CHANNEL>.<insMaxLcode>"`:
- opcode **1 = subscribe**, **2 = unsubscribe**, **3 = request/one-shot**.
- Channels seen: **`MW`** (Market Watch — the orderbook/queue; this is the one we
  need), `TP` (trade price), `MDG`, `ATH`/`Q:` (trades history / queue count),
  `IN` (indexes), `CFC`/`CFW` (derivatives).
- The subscribe constant is literally:
  ```
  SUBSCRIBE_ON_INSTRUMENTS   = "1,MW."     // subscribe to an instrument's market-watch
  UNSUBSCRIBE_ON_INSTRUMENTS = "2,MW."
  ```
  `subscribesInstrument(e)` builds `"1,MW." + e` and `.send()`s it.
- **Instrument key `e` = `insMaxLcode` = the ISIN** (e.g. `IRO1SROD0001`). Confirmed:
  `…insMaxLcode); this.subscribesInstrumentList(t)`.
- So to watch سرود's queue: `send("1,MW.IRO1SROD0001")`.

**Inbound** (`parseMessage(t)`): `const n = JSON.parse(t); const l = n.msgType;`
- Frames are **JSON** despite `encoding=text`. Common fields: `msgType`, `time`,
  `changeTime`. msgType values seen: `connect`, `time`, … and (TODO) the market-watch
  update type carrying the orderbook depth / best-buy-queue volume.

## The ONE remaining unknown → live probe

We have URL + auth + subscribe message + instrument key. The only thing the bundle
didn't hand us cleanly is **which field in the MW update frame is the best-buy-queue
share count** (the REST `bbq` equivalent from `StockInformationHandler`). Get it by a
**read-only live connect** (next step):

1. Exir login with the Khobregan account (reuse `exir_adapter` / `exir_spike.py`
   login) → `authToken`.
2. `wss://khobregan.exirbroker.com/sle/v2/ws?encoding=text&authToken=<token>&device=web`.
3. On open, `send("1,MW.IRO1SROD0001")` (سرود — a known live instrument).
4. Log every JSON frame; find the field whose value == the REST `bbq` for that ISIN
   (cross-check against `rlc_market.get_queue('IRO1SROD0001')['buy_volume']`).
5. Confirm whether ONE Khobregan account allows N concurrent WS sessions (open two) —
   decides the per-host-vs-global topology risk.

Spike script to write: `SellerMarket/scratch/rlc_ws_spike.py` (env-var creds like
`exir_spike.py`; `websockets` lib). Run from an Iranian-egress host (the WS host is
`khobregan.exirbroker.com`, already 200 from the local box and the VPSes).

## Design impact (vs the original plan)

- **Phase 1 simplifies**: the per-host market-data WS service is a thin raw-WS client
  (login → open `/sle/v2/ws` → `send("1,MW.<ISIN>")` per auto-sell ISIN → parse JSON
  frames → push `buy_volume` to local bots). No Lightstreamer dependency.
- Reuse the existing Exir login (`exir_adapter`) verbatim for the `authToken`.
- Bundle filenames captured (for re-pull): `main-es2015.7c13d0d9a07004a94517.js`,
  `vendor-es2015.776b86667b2969411938.js`, `scripts.03ebd774d0c1fe5f057e.js`.

## Reachability notes

- `khobregan.exirbroker.com` → HTTP 200 in ~1.5s from the local Windows host (Iran).
- The WS host == the Exir tenant host, so the same reachability that already lets
  `exir_adapter` log in covers the WS. No `push*.rhbroker.ir` / tsetmc dependency.
