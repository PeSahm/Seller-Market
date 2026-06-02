"""Free TSE (tsetmc.com) market-data client — the daily allowed price band.

Exir has NO REST price endpoint (its prices stream over Lightstreamer), so for
an Exir order the bot reads the daily *allowed price band* from the Tehran Stock
Exchange's public site tsetmc.com — free, no auth, a single HTTP GET.

``MarketWatchInit`` returns every instrument in one response; section [2] is a
``;``-separated list of ``,``-delimited rows where (full rows, len > 20):

    f[0]  = insCode (TSE id)        f[1]  = ISIN          f[2] = symbol
    f[13] = yesterday close         f[19] = UPPER band    f[20] = LOWER band

The upper band (``psGelStaMax`` / ``tmax``) is the day's BUY ceiling — the price
the bot fires a BUY at to sit head-of-queue at limit-up. The lower band is the
SELL floor. Numbers arrive like ``"9930.00"`` so we parse ``int(float(x))``.

The snapshot is cached (the static thresholds don't change intraday); refreshed
at most every ``_TTL_S``. Confirmed live: GetInstrumentInfo's
``staticThreshold.psGelStaMax`` == this row's f[19] (e.g. سرود 9930).
"""
from __future__ import annotations

import threading
import time

import requests

_MW_URL = "https://old.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0"
# tsetmc blocks non-browser agents; mirror the decompiled client's UA.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
_TTL_S = 300.0  # refresh the all-instrument snapshot at most every 5 minutes

_lock = threading.Lock()
_cache: dict[str, tuple[int, int]] = {}  # ISIN -> (ceiling, floor)
_loaded_at = 0.0


def _parse_market_watch(text: str) -> dict[str, tuple[int, int]]:
    """Parse a MarketWatchInit body into ``{ISIN: (ceiling, floor)}``."""
    out: dict[str, tuple[int, int]] = {}
    sections = text.split("@")
    if len(sections) < 3:
        return out
    for row in sections[2].split(";"):
        f = row.split(",")
        if len(f) <= 20:
            continue  # short/incremental row — no identity columns
        isin = f[1]
        if not isin:
            continue
        try:
            ceiling = int(float(f[19]))
            floor = int(float(f[20]))
        except (ValueError, IndexError):
            continue
        if ceiling > 0:
            out[isin] = (ceiling, floor)
    return out


def _ensure_loaded(timeout: int = 15) -> dict[str, tuple[int, int]]:
    global _cache, _loaded_at
    with _lock:
        if _cache and (time.monotonic() - _loaded_at) < _TTL_S:
            return _cache
        resp = requests.get(_MW_URL, headers={"User-Agent": _UA}, timeout=timeout)
        resp.raise_for_status()
        parsed = _parse_market_watch(resp.text)
        if parsed:
            _cache = parsed
            _loaded_at = time.monotonic()
        elif not _cache:
            raise RuntimeError("tsetmc MarketWatchInit returned no parseable rows")
        return _cache


def get_price_band(isin: str, timeout: int = 15) -> tuple[int, int]:
    """Return ``(ceiling, floor)`` allowed prices for ``isin`` from tsetmc.

    ``ceiling`` (upper threshold) is the BUY price; ``floor`` is the SELL price.
    Raises ``ValueError`` if the instrument isn't in the snapshot.
    """
    band = _ensure_loaded(timeout).get(isin)
    if band is None:
        raise ValueError(f"tsetmc: no price band for ISIN {isin!r}")
    return band


def clear_cache() -> None:
    """Drop the cached snapshot (tests)."""
    global _cache, _loaded_at
    with _lock:
        _cache = {}
        _loaded_at = 0.0
