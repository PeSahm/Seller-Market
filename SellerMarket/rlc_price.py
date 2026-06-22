"""Broker-native (RLC / Tadbir) market-data client — the daily allowed price band.

Exir / Rayan-HamAfza streams live prices over Lightstreamer, but the *same* RLC
market-data backend that powers the broker also exposes a **public REST handler**
that returns the daily allowed price band (the static thresholds) for any
instrument, keyed by ISIN. This keeps each trading VPS self-contained: the band
comes from the broker's own infrastructure — no tsetmc.com, no cross-VPS relay.

Why not tsetmc: tsetmc's edge hard-blocks the Iranian trading hosts at the IP
layer (TCP reset / timeout), and routing it through the VPS's foreign proxy is
exactly the cross-VPS / external dependency we want to avoid. ``core.tadbirrlc.com``
(``193.34.245.250``) is an Iranian host reachable **directly** (no proxy) from
the bot container — confirmed live from the PouyanIt host and from inside the
running bot container.

Endpoint (confirmed live, no auth — public GET):

    GET https://core.tadbirrlc.com//StockInformationHandler
        ?{'Type':'getstockprice2','la':'Fa','arr':'<ISIN[,ISIN...]>'}&jsoncallback=

    -> JSON array, one object per instrument:
         nc  = instrument code (== the ISIN we queried)
         hap = upper allowed price  (the BUY ceiling — limit-up)
         lap = lower allowed price  (the SELL floor — limit-down)
         cp/ltp/pcp = close / last-traded / yesterday (unused here)

``hap`` is the day's BUY ceiling — the price the bot fires a BUY at to sit
head-of-queue at limit-up; ``lap`` is the SELL floor. Values arrive like
``"9930.0000000000000"`` so we parse ``int(float(x))``. Confirmed live: سرود
(``IRO1SROD0001``) ``hap=9930 lap=9370`` — identical to tsetmc's
``psGelStaMax``/``psGelStaMin`` for the same instrument, so this is a byte-clean
drop-in for the retired :mod:`tse_price`.

The band is static intraday, so a small per-ISIN cache (``_TTL_S``) avoids
redundant calls when several accounts share one instrument.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse

import requests

import runtime_config

logger = logging.getLogger(__name__)

# The RLC market-data gateway. The doubled slash mirrors the broker client's own
# URL (the handler is tolerant of it) — kept verbatim to match what the server
# expects from the official desktop client. Read at call-time from the DB-pushed
# [runtime] section (fallback = this literal) so the host can be redirected
# fleet-wide with NO image rebuild; matters for the long-running sidecar, which
# would otherwise hold this import-time constant until a restart.
_BASE_URL = "https://core.tadbirrlc.com//StockInformationHandler"
# Mirror a browser UA; some RLC edges reject bare clients.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
_TTL_S = 300.0  # bands are static intraday; refresh at most every 5 minutes

# A dedicated session with ``trust_env=False`` so this Iranian host is ALWAYS
# reached directly, never routed through the VPS's foreign HTTP proxy (which
# can't reach it). This makes the price fetch independent of /etc/environment.
_session = requests.Session()
_session.trust_env = False
_session.headers.update({"User-Agent": _UA})

_lock = threading.Lock()
# isin -> ((ceiling, floor, max_order_qty), monotonic_loaded_at)
_cache: dict[str, tuple[tuple[int, int, int], float]] = {}


def _build_url(isins: list[str]) -> str:
    """Build the StockInformationHandler URL for one or more ISINs."""
    base = runtime_config.get("rlc_base_url", _BASE_URL)
    blob = "{'Type':'getstockprice2','la':'Fa','arr':'" + ",".join(isins) + "'}"
    return base + "?" + urllib.parse.quote(blob) + "&jsoncallback="


def _parse_rows(rows: object) -> dict[str, tuple[int, int, int]]:
    """Parse the handler's JSON array into ``{ISIN: (ceiling, floor, max_order_qty)}``."""
    out: dict[str, tuple[int, int, int]] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        nc = row.get("nc")
        if not nc:
            continue
        try:
            ceiling = int(float(row.get("hap")))  # upper threshold (BUY ceiling)
            floor = int(float(row.get("lap")))    # lower threshold (SELL floor)
        except (TypeError, ValueError):
            continue
        # mxqo = the broker's MAX ORDER QUANTITY (the volume upper threshold it
        # rejects orders for). 0 when absent/unparseable → "no cap".
        try:
            max_qty = max(0, int(float(row.get("mxqo"))))
        except (TypeError, ValueError):
            max_qty = 0
        if ceiling > 0:
            out[nc] = (ceiling, floor, max_qty)
    return out


def _fetch(isins: list[str], timeout: int) -> dict[str, tuple[int, int, int]]:
    """Fetch + parse the band for ``isins`` (no caching). Raises on transport."""
    resp = _session.get(_build_url(isins), timeout=timeout)
    resp.raise_for_status()
    return _parse_rows(json.loads(resp.text))


def prefetch(isins: list[str], timeout: int = 15) -> None:
    """Warm the cache for several ISINs in one request (best-effort).

    Transport/parse failures are swallowed (logged) — the per-ISIN
    ``get_price_band`` retries on demand, so a failed warmup must never abort
    the caller (e.g. the bot's pre-run warmup).
    """
    unique = sorted({i for i in isins if i})
    if not unique:
        return
    try:
        parsed = _fetch(unique, timeout)
    except Exception as exc:  # noqa: BLE001 — warmup is best-effort; get_price_band retries
        logger.warning("rlc_price prefetch failed for %d ISIN(s): %s", len(unique), exc)
        return
    if parsed:
        now = time.monotonic()
        with _lock:
            for isin, info in parsed.items():
                _cache[isin] = (info, now)


def _get_info(isin: str, timeout: int = 15) -> tuple[int, int, int]:
    """Return cached ``(ceiling, floor, max_order_qty)`` for ``isin`` (fetch on miss).

    Raises ``ValueError`` if the instrument isn't in the handler's response.
    """
    with _lock:
        hit = _cache.get(isin)
        if hit is not None and (time.monotonic() - hit[1]) < _TTL_S:
            return hit[0]
    # Network OUTSIDE the lock so a slow fetch can't stall other accounts.
    parsed = _fetch([isin], timeout)
    info = parsed.get(isin)
    if info is None:
        raise ValueError(f"rlc: no price band for ISIN {isin!r}")
    with _lock:
        _cache[isin] = (info, time.monotonic())
    return info


def get_price_band(isin: str, timeout: int = 15) -> tuple[int, int]:
    """Return ``(ceiling, floor)`` allowed prices for ``isin`` from the broker's
    own RLC market-data gateway.

    ``ceiling`` (upper threshold / ``hap``) is the BUY price; ``floor`` (lower /
    ``lap``) is the SELL price. Raises ``ValueError`` if the instrument isn't in
    the handler's response.
    """
    ceiling, floor, _ = _get_info(isin, timeout)
    return ceiling, floor


def get_max_order_qty(isin: str, timeout: int = 15) -> int:
    """Return the instrument's MAX ORDER QUANTITY (``mxqo``) — the per-order
    volume upper threshold the broker enforces.

    Returns 0 when unknown (instrument missing, ``mxqo`` absent, or a transient
    fetch error) so a data gap is treated as "no cap" and never blocks an order.
    Shares the cache with :func:`get_price_band`, so calling it right after the
    price band is a free cache hit.
    """
    try:
        return _get_info(isin, timeout)[2]
    except (ValueError, requests.RequestException):
        return 0


def clear_cache() -> None:
    """Drop the cached bands (tests)."""
    with _lock:
        _cache.clear()
