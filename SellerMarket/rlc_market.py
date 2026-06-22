"""Broker-native (RLC / Tadbir) market-data client for the per-host sidecar.

The market-data **sidecar** (one container per VPS, deployed by the mgmt UI)
serves price band, last price, the full instrument list, and the buy/sell queue
to every local bot + the local mgmt UI — for ALL brokers, since this data is
market-wide. The single source is the shared RLC backend ``core.tadbirrlc.com``
(the same one :mod:`rlc_price` already uses for the price band), reached
**directly** (``trust_env=False``) so a foreign HTTP proxy on the host can never
intercept the Iranian endpoint.

Endpoints (all on ``core.tadbirrlc.com``):

* ``StockInformationHandler`` — ``{'Type':'getstockprice2','la':'Fa','arr':'<ISIN[,...]>'}``
  → per-instrument row: ``nc`` (ISIN), ``cn`` (company), ``sf`` (symbol),
  ``hap``/``lap`` (band), ``mxqo`` (max order qty), ``cp``/``ltp``/``pcp``
  (close / last-traded / yesterday). **CONFIRMED public, no auth** — this powers
  the price band (via :mod:`rlc_price`) and the **last price** here.
* ``StocksHandler.ashx`` — ``{"Type":"ALL21"}`` → the whole-market instrument
  list. Used for the name↔ISIN dropdown. Shape is parsed **tolerantly** (dict
  rows or delimited strings) and a raw sample is logged the first time so the
  field mapping can be confirmed live without a rebuild.
* ``StockFutureInfoHandler`` — ``{'Type':'getLightSymbolInfoAndQueue','la':'Fa',
  'nscCode':'<ISIN>'}`` → single-symbol band + order **queue**. Used by auto-sell
  (the buy-queue share count). Parsed tolerantly; raw logged for live tuning.

Confirmed parts (band, last price) are solid; the instrument-list + queue parsers
are best-effort and NEVER raise out of the public helpers — they degrade to
``[]`` / ``None`` and log, so the sidecar stays up while the exact shape is
pinned against a live Khobregan session.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse

import requests

import rlc_price  # reuse the confirmed session + price-band/max-qty helpers
import runtime_config

logger = logging.getLogger(__name__)

# Fallback host + handler paths. Read at call-time from the DB-pushed [runtime]
# section (``rlc_market_host`` / ``rlc_path_*``) so the long-running sidecar can
# be redirected fleet-wide with NO image rebuild. The host keeps its trailing
# slash and each path its leading slash so the concatenation reproduces the
# doubled slash the official client uses.
_HOST = "https://core.tadbirrlc.com/"
_PATH_STOCK_INFO = "/StockInformationHandler"
_PATH_STOCKS = "/StocksHandler.ashx"
_PATH_FUTURE_INFO = "/StockFutureInfoHandler"


def _host() -> str:
    return runtime_config.get("rlc_market_host", _HOST)


def _stock_info_url() -> str:
    return _host() + runtime_config.get("rlc_path_stockinfo", _PATH_STOCK_INFO)


def _stocks_url() -> str:
    return _host() + runtime_config.get("rlc_path_stocks", _PATH_STOCKS)


def _future_info_url() -> str:
    return _host() + runtime_config.get("rlc_path_future", _PATH_FUTURE_INFO)

# Reuse rlc_price's proxy-bypassed session (trust_env=False + browser UA) so we
# don't open a second connection pool or re-declare the bypass.
_session = rlc_price._session

# Caches. Bands/last-price are static-ish intraday; the instrument list barely
# changes day to day; the queue moves second-to-second so it gets a tiny TTL.
_LAST_TTL_S = 60.0
_INSTRUMENTS_TTL_S = 6 * 3600.0
_QUEUE_TTL_S = 2.0

_lock = threading.Lock()
_last_cache: dict[str, tuple[int, float]] = {}            # isin -> (last_price, loaded_at)
_queue_cache: dict[str, tuple[dict, float]] = {}          # isin -> (queue_dict, loaded_at)
_instruments_cache: dict[str, object] = {"rows": None, "at": 0.0}  # full list + load time

_logged_raw: set[str] = set()  # one-time raw-sample logging guard per endpoint


def _log_raw_once(tag: str, payload: object) -> None:
    """Log a trimmed raw sample of a response shape ONCE per endpoint (live tuning)."""
    if tag in _logged_raw:
        return
    _logged_raw.add(tag)
    sample = json.dumps(payload)[:1500] if not isinstance(payload, str) else payload[:1500]
    logger.info("rlc_market raw sample [%s]: %s", tag, sample)


def _blob(d: dict) -> str:
    """RLC-style single-quoted JSON-ish blob, URL-encoded, with the trailing cb."""
    inner = "{" + ",".join(f"'{k}':'{v}'" for k, v in d.items()) + "}"
    return urllib.parse.quote(inner) + "&jsoncallback="


# ---------------------------------------------------------------------------
# Price band + max order qty — delegate to the confirmed rlc_price client.
# ---------------------------------------------------------------------------

def get_price_band(isin: str, timeout: int = 15) -> tuple[int, int]:
    """``(ceiling, floor)`` allowed prices for ``isin`` (BUY ceiling / SELL floor)."""
    return rlc_price.get_price_band(isin, timeout)


def get_max_order_qty(isin: str, timeout: int = 15) -> int:
    """Per-order MAX volume (``mxqo``) the broker enforces; 0 = unknown/no cap."""
    return rlc_price.get_max_order_qty(isin, timeout)


# ---------------------------------------------------------------------------
# Last price (CONFIRMED fields via StockInformationHandler).
# ---------------------------------------------------------------------------

def get_last_price(isin: str, timeout: int = 15) -> int:
    """Last-traded price for ``isin`` (``ltp``, falling back to ``cp`` close then
    ``pcp`` yesterday-close). Used for the fee 20-day mark-to-market.

    Returns 0 when unknown (instrument missing or all price fields absent).
    """
    with _lock:
        hit = _last_cache.get(isin)
        if hit is not None and (time.monotonic() - hit[1]) < _LAST_TTL_S:
            return hit[0]
    url = _stock_info_url() + "?" + _blob(
        {"Type": "getstockprice2", "la": "Fa", "arr": isin}
    )
    try:
        resp = _session.get(url, timeout=timeout)
        resp.raise_for_status()
        rows = json.loads(resp.text)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("rlc_market last-price fetch failed for %s: %s", isin, exc)
        return 0
    last = 0
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict) or row.get("nc") != isin:
                continue
            for field in ("ltp", "cp", "pcp"):
                try:
                    v = int(float(row.get(field)))
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    last = v
                    break
            break
    if last > 0:
        with _lock:
            _last_cache[isin] = (last, time.monotonic())
    return last


# ---------------------------------------------------------------------------
# Full instrument list (tolerant — ALL21 shape confirmed live, parser hedged).
# ---------------------------------------------------------------------------

def _parse_instrument_row(row: object) -> dict | None:
    """Best-effort map of one ALL21 row → ``{isin, symbol, name}`` (or None)."""
    if isinstance(row, dict):
        isin = row.get("nc") or row.get("InsCode") or row.get("isin")
        name = row.get("cn") or row.get("name") or row.get("Title")
        symbol = row.get("sf") or row.get("symbol") or row.get("Symbol")
        if isin:
            return {"isin": str(isin), "symbol": str(symbol or ""), "name": str(name or "")}
        return None
    if isinstance(row, str):
        # Some RLC handlers return comma- or pipe-delimited strings. Heuristic:
        # find an ISIN-looking field (12 chars, starts "IR") and take neighbours.
        parts = [p.strip() for p in row.replace("|", ",").split(",")]
        isin = next((p for p in parts if len(p) == 12 and p[:2].upper() == "IR"), None)
        if not isin:
            return None
        others = [p for p in parts if p and p != isin]
        symbol = others[0] if others else ""
        name = others[1] if len(others) > 1 else ""
        return {"isin": isin, "symbol": symbol, "name": name}
    return None


def get_instruments(timeout: int = 30, force: bool = False) -> list[dict]:
    """Whole-market instrument list ``[{isin, symbol, name}, ...]`` (cached daily).

    Best-effort: on any transport/parse failure returns the last good cache (or
    ``[]``) and logs — never raises.
    """
    with _lock:
        rows = _instruments_cache.get("rows")
        at = float(_instruments_cache.get("at") or 0.0)
        if rows is not None and not force and (time.monotonic() - at) < _INSTRUMENTS_TTL_S:
            return rows  # type: ignore[return-value]
    url = _stocks_url() + "?" + _blob({"Type": "ALL21"})
    try:
        resp = _session.get(url, timeout=timeout)
        resp.raise_for_status()
        payload = json.loads(resp.text)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("rlc_market instrument-list fetch failed: %s", exc)
        with _lock:
            return _instruments_cache.get("rows") or []  # type: ignore[return-value]
    _log_raw_once("ALL21", payload[:3] if isinstance(payload, list) else payload)
    out: list[dict] = []
    if isinstance(payload, list):
        for row in payload:
            parsed = _parse_instrument_row(row)
            if parsed:
                out.append(parsed)
    if out:
        with _lock:
            _instruments_cache["rows"] = out
            _instruments_cache["at"] = time.monotonic()
        logger.info("rlc_market instrument list refreshed: %d instruments", len(out))
        return out
    with _lock:
        return _instruments_cache.get("rows") or []  # type: ignore[return-value]


def search_instruments(q: str, limit: int = 20) -> list[dict]:
    """Case-insensitive name/symbol/ISIN search over the cached instrument list."""
    q = (q or "").strip().lower()
    if not q:
        return []
    rows = get_instruments()
    hits = [
        r for r in rows
        if q in r["name"].lower() or q in r["symbol"].lower() or q in r["isin"].lower()
    ]
    # Prefer prefix matches on symbol/name, then the rest; stable + capped.
    hits.sort(key=lambda r: (
        not (r["symbol"].lower().startswith(q) or r["name"].lower().startswith(q)),
        r["symbol"],
    ))
    return hits[:limit]


# ---------------------------------------------------------------------------
# Order queue — the best-level (top-of-book) buy/sell queue.
#
# LIVE-CONFIRMED shape: the StockInformationHandler row (getstockprice2) carries
# the best-level queue directly — ``bbq`` (best-BUY quantity = the buy-queue /
# صف خرید volume), ``bsq`` (best-SELL quantity), ``nbb``/``nbs`` (order counts),
# ``bbp``/``bsp`` (best buy/sell price). One call also gives band + last, so we
# reuse the same handler the price band uses. (StockFutureInfoHandler returns
# ``symbolinfo`` + ``symbolqueue.Value`` for the FULL 5-level depth, but the
# best level here is what auto-sell needs; the depth handler stays a future
# refinement.)
# ---------------------------------------------------------------------------

def _extract_queue(payload: object, isin: str) -> dict | None:
    """Best-level buy/sell queue from a getstockprice2 row for ``isin``.

    Returns ``{buy_volume, sell_volume, buy_count, sell_count, best_buy_price,
    best_sell_price}`` or None. ``buy_volume`` (``bbq``) is the buy-queue share
    count auto-sell compares against its threshold.
    """
    rows = payload
    if not isinstance(rows, list):
        return None
    row = next((r for r in rows if isinstance(r, dict) and r.get("nc") == isin), None)
    if row is None:
        return None

    def _num(*keys):
        for k in keys:
            v = row.get(k)
            if v is None:
                continue
            try:
                return int(float(v))
            except (TypeError, ValueError):
                continue
        return None

    return {
        "buy_volume": _num("bbq") or 0,    # best-buy qty = buy-queue volume
        "sell_volume": _num("bsq") or 0,   # best-sell qty = sell-queue volume
        "buy_count": _num("nbb") or 0,
        "sell_count": _num("nbs") or 0,
        "best_buy_price": _num("bbp") or 0,
        "best_sell_price": _num("bsp") or 0,
    }


def get_queue(isin: str, timeout: int = 10) -> dict | None:
    """Best-level order-queue snapshot for ``isin`` (``{buy_volume, sell_volume,
    ...}``).

    Used by auto-sell (sell when the buy-queue share count ``buy_volume`` drops
    below a threshold). 2s TTL. Returns None when unavailable (logged, never
    raises) so the caller holds rather than sells on missing data.
    """
    with _lock:
        hit = _queue_cache.get(isin)
        if hit is not None and (time.monotonic() - hit[1]) < _QUEUE_TTL_S:
            return hit[0]
    url = _stock_info_url() + "?" + _blob(
        {"Type": "getstockprice2", "la": "Fa", "arr": isin}
    )
    try:
        resp = _session.get(url, timeout=timeout)
        resp.raise_for_status()
        payload = json.loads(resp.text)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("rlc_market queue fetch failed for %s: %s", isin, exc)
        return None
    _log_raw_once("queue", payload[:1] if isinstance(payload, list) else payload)
    q = _extract_queue(payload, isin)
    if q is not None:
        with _lock:
            _queue_cache[isin] = (q, time.monotonic())
    return q


def clear_cache() -> None:
    """Drop all caches (tests)."""
    with _lock:
        _last_cache.clear()
        _queue_cache.clear()
        _instruments_cache["rows"] = None
        _instruments_cache["at"] = 0.0
    _logged_raw.clear()
