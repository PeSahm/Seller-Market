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


async def _base_url(db: AsyncSession) -> str:
    try:
        url = await settings_store.get_setting(db, "market_data_url")
        return (url or _DEFAULT_BASE).rstrip("/")
    except Exception:  # noqa: BLE001 — a missing/garbled setting shouldn't 500
        return _DEFAULT_BASE


async def search_instruments(db: AsyncSession, q: str, limit: int = 20) -> list[dict]:
    """``[{isin, symbol, name}, ...]`` matching ``q`` (name/symbol/ISIN).

    Returns ``[]`` for short/empty queries or any sidecar failure.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return []
    base = await _base_url(db)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/search", params={"q": q, "limit": limit})
            r.raise_for_status()
            return (r.json() or {}).get("instruments") or []
    except Exception as exc:  # noqa: BLE001 — graceful: no suggestions
        logger.warning("market-data search failed (q=%r): %s", q, exc)
        return []


async def get_last_price(db: AsyncSession, isin: str) -> Optional[int]:
    """Last-traded price for ``isin`` (for the fee 20-day mark-to-market), or None."""
    base = await _base_url(db)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/last-price", params={"isin": isin})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            val = int((r.json() or {}).get("last_price") or 0)
            return val or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("market-data last-price failed (isin=%s): %s", isin, exc)
        return None


async def get_price_band(db: AsyncSession, isin: str) -> Optional[tuple[int, int]]:
    """``(ceiling, floor)`` allowed prices for ``isin``, or None on any failure."""
    base = await _base_url(db)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/price-band", params={"isin": isin})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json() or {}
            return int(data.get("ceiling") or 0), int(data.get("floor") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("market-data price-band failed (isin=%s): %s", isin, exc)
        return None


async def get_queue(db: AsyncSession, isin: str) -> Optional[dict]:
    """Best-level order queue for ``isin`` (``{buy_volume, sell_volume, ...}``), or None."""
    base = await _base_url(db)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/queue", params={"isin": isin})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("market-data queue failed (isin=%s): %s", isin, exc)
        return None


async def get_instruments(db: AsyncSession) -> list[dict]:
    """Whole-market instrument list ``[{isin, symbol, name}, ...]`` from the
    sidecar ``/instruments`` — used to warm the ISIN→name cache.

    Returns ``[]`` on any failure (graceful; the resolver then falls back to the
    ``broker_orders`` supplement / bare ISIN).
    """
    base = await _base_url(db)
    try:
        async with httpx.AsyncClient(timeout=_INSTRUMENTS_TIMEOUT) as client:
            r = await client.get(f"{base}/instruments")
            r.raise_for_status()
            return (r.json() or {}).get("instruments") or []
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
