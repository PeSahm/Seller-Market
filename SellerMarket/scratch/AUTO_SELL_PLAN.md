# Issue #110 — Auto-sell on a thinning buy-queue (approved plan: backend + UX)

> Design doc for review. Implementation proceeds in phases, each its own PR.

## Context

The bot should **automatically sell a held position** when an instrument's
**best-buy-queue share count** drops to/below a **per-instrument threshold**,
watched in real time over the broker's RLC **WebSocket** (push, not polling).
The sell is placed by **new direct code (NOT locust)**, **always at the floor
(lowest day price)**, **chunked to the per-order max volume**.

**Pricing/chunking rule (operator's example, exact):** band = [5, 20] → floor = 5.
Holdings 1001, max order volume 100 → fire **10 orders of (volume 100, price 5)
+ 1 order of (volume 1, price 5)**. Every chunk at the floor; split by max volume.

**UX the operator wants:**

- When adding/editing a **Buy** trade instruction, surface an **auto-sell option**
  (the threshold field) — shown only when Side = Buy.
- An **"Active auto-sell" page** (admin + agent): armed positions, live buy-queue,
  and which fired today.
- A **live queue-status** view (live is preferred; auto-refresh acceptable).

The WS protocol was **cracked from the Exir SPA bundle** — see
`RLC_WS_FINDINGS.md` in this folder. It is **NOT Lightstreamer**: a plain JSON
WebSocket at `wss://<tenant>.exirbroker.com/sle/v2/ws?encoding=text&authToken=<token>&device=web`,
subscribe with text frame `"1,MW.<ISIN>"`, auth = the **Exir `authToken` already
obtained at login**. The only unconfirmed bit is which MW-frame field is the
buy-queue count — the read-only probe `rlc_ws_spike.py` (this folder) pins it.

## Decisions (locked with the operator)

- Queue = **RLC WebSocket push**. **One shared WS service on PouyanIt
  (`5.10.248.55`)**, co-located with mgmt + OCR (every bot already calls PouyanIt
  for OCR at `:18080`, so this rides the same cross-host pattern; one Khobregan
  connection avoids the multi-IP lock). Auth = the Mostafa/Khobregan Exir account.
- Threshold = **per-instruction** nullable `auto_sell_threshold` (a buy-queue SHARE
  COUNT) on `trade_instructions`. Surfaced on the form **only for Buy (side=1)**.
- Sell = **full holdings, all chunks at the floor**, split by the per-order max
  volume; re-read live holdings so partial fills re-fire until flat; one position/day.
- Sell placed by **direct HTTP** (reuse adapter auth + body; NOT locust).
- Monitor runs **inside the bot container** (`bot_entrypoint.py`: scheduler thread +
  monitor foreground — mirrors `simple_config_bot.py::main()`).
- mgmt UI gets a conditional form field + an **Active auto-sell** page + a **live
  queue** view (mirror the existing browser-WS pattern; auto-refresh fallback).

## Architecture

```text
PouyanIt 5.10.248.55
  ├ mgmt UI (api) ── relays sidecar push to the browser for the live queue page
  └ market-data sidecar (ENHANCED)
      • existing Flask REST (/queue,/price-band,…) UNCHANGED  (mgmt pages reuse it)
      • NEW upstream Khobregan WS → wss://khobregan.exirbroker.com/sle/v2/ws,
            subscribes "1,MW.<ISIN>" for the union of armed ISINs
      • NEW local fan-out push  ws://5.10.248.55:<port>/ws/queue?isin=… → {isin,buy_volume}
        ▲ host-published port (like OCR :18080); MARKET_DATA_URL=http://5.10.248.55:<port>
  bots (every VPS) ── bot container
      • bot_entrypoint.py: JobScheduler.start()[bg] + AutoSellMonitor.run()[fg]
      • on buy_volume ≤ threshold → direct chunked SELL at floor (customer creds)
      • emits side=2 fire-log + an auto_sell state line (run_results/)
```

## Phases

- **Phase 0b — live WS probe** (`rlc_ws_spike.py`, read-only, no orders): confirm the
  MW-frame buy-queue field == REST `bbq`; test the concurrent-session limit. GATES Phase 1.
- **Phase 1 — Shared WS service on PouyanIt**: new `rlc_ws.py` (websocket-client upstream,
  reuse `exir_adapter._login`), add a local fan-out push `/ws/queue` to `market_data_app.py`,
  add `websocket-client` to requirements, host-publish the port + Khobregan env.
- **Phase 2 — Threshold plumbing + conditional form UX**: migration
  `0012_ti_auto_sell_threshold` (nullable int), model/schema/service, the **Buy-only**
  form field + JS toggle, render `auto_sell_threshold` into config.ini.
- **Phase 3 — Bot monitor + direct chunked SELL**: `order_fire_log.py` (extract the fire
  writer), `broker_adapters.prepare_chunk`/`open_session`, per-family `_build_body`,
  `direct_sell.send_prepared_order`, `auto_sell_engine.chunk_volumes`/`sell_entire_position`
  (all chunks at floor; 1001/100 → 10×100+1), `auto_sell_monitor.py` (lazy login,
  market-hours gate, fail-safe HOLD, idempotent day-state), `bot_entrypoint.py`; switch the
  bot `command:` + add `MARKET_DATA_URL`.
- **Phase 4 — Auto-sell UX pages**: `/admin/auto-sell` + `/agent/auto-sell` (armed positions
  + live buy-queue + fired-today), live queue view (browser-WS relay; HTMX 3s fallback).
- **Phase 5 — Deploy + canary** on Mostafa's سرود Buy instruction, then fleet.

## Key risks / mitigations

- Login/captcha storm → lazy per-account login on first trigger.
- Feed down/stale → `buy_volume` None ⇒ HOLD; never sell on missing data.
- Restart double-sell → day-state file + live-holdings re-read before every ladder.
- 300ms order rate limit (1018/1005) → ≥350ms chunk spacing + backoff.
- Bad floor (≤0)/missing max-vol → abort on floor≤0; no cap ⇒ single order.
- Spikes to confirm during build: MW buy-queue field (0b); SELL rate-limit codes == BUY;
  ephoenix per-order SELL cap == `instrument_info['max_volume']`; the local push schema.
