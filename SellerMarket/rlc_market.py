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

logger = logging.getLogger(__name__)

_HOST = "https://core.tadbirrlc.com/"
_STOCK_INFO_URL = _HOST + "/StockInformationHandler"
_STOCKS_URL = _HOST + "/StocksHandler.ashx"
_FUTURE_INFO_URL = _HOST + "/StockFutureInfoHandler"

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
    url = _STOCK_INFO_URL + "?" + _blob(
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
    url = _STOCKS_URL + "?" + _blob({"Type": "ALL21"})
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
# Order queue (tolerant — shape pinned live; raw logged; never raises).
# ---------------------------------------------------------------------------

def _extract_queue(payload: object) -> dict | None:
    """Best-effort buy/sell queue volumes from a StockFutureInfoHandler payload.

    Returns ``{buy_volume, sell_volume, buy_count, sell_count, raw}`` or None.
    Field names are hedged across the shapes seen in the decompiled
    ``TadbirSymbolDataFetcher`` (``zd``/``qd`` = best-bid count/volume,
    ``zo``/``qo`` = best-ask count/volume; ``symbolinfo`` wrapper).
    """
    obj = payload
    if isinstance(obj, list) and obj:
        obj = obj[0]
    if isinstance(obj, dict) and isinstance(obj.get("symbolinfo"), dict):
        obj = obj["symbolinfo"]
    if not isinstance(obj, dict):
        return None

    def _num(*keys):
        for k in keys:
            v = obj.get(k)
            if v is None:
                continue
            try:
                return int(float(v))
            except (TypeError, ValueError):
                continue
        return None

    buy_volume = _num("qd", "bestBuyQuantity", "totalBuyVolume", "buyQueueVolume", "qbd")
    sell_volume = _num("qo", "bestSellQuantity", "totalSellVolume", "sellQueueVolume", "qbo")
    if buy_volume is None and sell_volume is None:
        return None
    return {
        "buy_volume": buy_volume or 0,
        "sell_volume": sell_volume or 0,
        "buy_count": _num("zd", "buyCount") or 0,
        "sell_count": _num("zo", "sellCount") or 0,
        "raw": obj,
    }


def get_queue(isin: str, timeout: int = 10) -> dict | None:
    """Order-queue snapshot for ``isin`` (``{buy_volume, sell_volume, ...}``).

    Used by auto-sell (sell when the buy-queue share count drops below a
    threshold). 2s TTL. Returns None when unavailable (logged, never raises) so
    the caller can decide (auto-sell holds rather than sells on missing data).
    """
    with _lock:
        hit = _queue_cache.get(isin)
        if hit is not None and (time.monotonic() - hit[1]) < _QUEUE_TTL_S:
            return hit[0]
    url = _FUTURE_INFO_URL + "?" + _blob(
        {"Type": "getLightSymbolInfoAndQueue", "la": "Fa", "nscCode": isin}
    )
    try:
        resp = _session.get(url, timeout=timeout)
        resp.raise_for_status()
        payload = json.loads(resp.text)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("rlc_market queue fetch failed for %s: %s", isin, exc)
        return None
    _log_raw_once("queue", payload)
    q = _extract_queue(payload)
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
