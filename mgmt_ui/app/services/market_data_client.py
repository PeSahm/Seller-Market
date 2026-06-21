"""Thin async client for the per-host market-data sidecar (issue #108).

The mgmt UI reaches its local sidecar (one per host; on the mgmt host it runs as
the ``market-data`` compose service) for instrument search, price band, last
price, and the order queue — the same shared RLC-backed source the bots use, so
the whole app has one consistent market-data source.

Every call **degrades gracefully** — a timeout / connection error / non-200
returns ``[]`` or ``None`` and logs a warning, never raising. A sidecar hiccup
must never 500 a page or block the fee report; the instrument dropdown simply
shows no suggestions (the manual ISIN field still works) and the fee 20-day
mark-to-market falls back to "no live price".
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import settings_store

logger = logging.getLogger(__name__)

# Short timeouts — these are local, fast calls; we never want them to stall a
# request-handling coroutine.
_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
# The whole-market list can be large and a cold sidecar may fetch ALL21 from RLC
# on the first hit, so give /instruments more headroom (it's a startup/6h call,
# not a hot path).
_INSTRUMENTS_TIMEOUT = httpx.Timeout(20.0, connect=3.0)
_DEFAULT_BASE = "http://market-data:8077"


def _parse_bases(raw: Optional[str]) -> list[str]:
    """Comma/space-separated ``market_data_url`` -> ordered failover pool.

    Mirrors the OCR pool (HA): a single URL yields one element; the list is tried
    in order, preferring the first. Trailing slashes stripped. Falls back to the
    compose-network default when unset/garbled.
    """
    parts = (raw or "").replace(",", " ").split()
    bases = [p.rstrip("/") for p in parts if p.strip()]
    return bases or [_DEFAULT_BASE]


async def _base_urls(db: AsyncSession) -> list[str]:
    try:
        raw = await settings_store.get_setting(db, "market_data_url")
    except Exception:  # noqa: BLE001 — a missing/garbled setting shouldn't 500
        return [_DEFAULT_BASE]
    return _parse_bases(raw)


async def _fetch(
    db: AsyncSession, path: str, *, params: Optional[dict] = None, timeout=_TIMEOUT
):
    """GET ``path`` from the market-data failover pool.

    Tries each configured base in order (prefer-primary), failing over to the
    next on ANY per-base error — a transport/HTTP error OR a malformed body
    (``r.json()`` raising) — so one bad sidecar can't shadow a healthy backup.
    Returns the parsed JSON body on the first healthy response, or ``None`` for a
    definitive **404** (a healthy sidecar saying "no data for this ISIN" — NOT a
    reason to fail over; the next host shares the same RLC source and would 404
    too). Raises the last error only if EVERY base failed; the callers swallow
    that into their graceful ``[]``/``None``.
    """
    bases = await _base_urls(db)
    last_exc: Optional[Exception] = None
    for base in bases:
        try:
            # trust_env=False: reach the (Iranian-host) sidecar directly, never
            # via a foreign HTTP proxy that may sit in the container env — the
            # belt-and-suspenders rule used by rlc_price/rlc_market on the bot.
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                r = await client.get(f"{base}{path}", params=params)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001 — any per-base error → try next
            last_exc = exc
            logger.warning(
                "market-data %s via %s failed, trying next: %s", path, base, exc
            )
            continue
    raise last_exc or httpx.HTTPError("no market-data endpoints configured")


async def search_instruments(db: AsyncSession, q: str, limit: int = 20) -> list[dict]:
    """``[{isin, symbol, name}, ...]`` matching ``q`` (name/symbol/ISIN).

    Returns ``[]`` for short/empty queries or any sidecar failure.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return []
    try:
        data = await _fetch(db, "/search", params={"q": q, "limit": limit})
        return (data or {}).get("instruments") or []
    except Exception as exc:  # noqa: BLE001 — graceful: no suggestions
        logger.warning("market-data search failed (q=%r): %s", q, exc)
        return []


async def get_last_price(db: AsyncSession, isin: str) -> Optional[int]:
    """Last-traded price for ``isin`` (for the fee 20-day mark-to-market), or None."""
    try:
        data = await _fetch(db, "/last-price", params={"isin": isin})
        if not data:
            return None
        val = int(data.get("last_price") or 0)
        return val or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("market-data last-price failed (isin=%s): %s", isin, exc)
        return None


async def get_price_band(db: AsyncSession, isin: str) -> Optional[tuple[int, int]]:
    """``(ceiling, floor)`` allowed prices for ``isin``, or None on any failure."""
    try:
        data = await _fetch(db, "/price-band", params={"isin": isin})
        if not data:
            return None
        return int(data.get("ceiling") or 0), int(data.get("floor") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("market-data price-band failed (isin=%s): %s", isin, exc)
        return None


async def get_queue(db: AsyncSession, isin: str) -> Optional[dict]:
    """Best-level order queue for ``isin`` (``{buy_volume, sell_volume, ...}``), or None."""
    try:
        data = await _fetch(db, "/queue", params={"isin": isin})
        return data or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("market-data queue failed (isin=%s): %s", isin, exc)
        return None


async def get_instruments(db: AsyncSession) -> list[dict]:
    """Whole-market instrument list ``[{isin, symbol, name}, ...]`` from the
    sidecar ``/instruments`` — used to warm the ISIN→name cache.

    Returns ``[]`` on any failure (graceful; the resolver then falls back to the
    ``broker_orders`` supplement / bare ISIN).
    """
    try:
        data = await _fetch(db, "/instruments", timeout=_INSTRUMENTS_TIMEOUT)
        return (data or {}).get("instruments") or []
    except Exception as exc:  # noqa: BLE001 — graceful: no names
        logger.warning("market-data instruments failed: %s", exc)
        return []


__all__ = [
    "search_instruments",
    "get_last_price",
    "get_price_band",
    "get_queue",
    "get_instruments",
]
