"""ISIN → human-readable symbol resolver (warm in-memory cache).

Agents see bare ISINs (``IRO3SMBZ0001``) in grids — meaningless. This module
resolves an ISIN to its symbol (``سرود``) / company name (``سیمان شاهرود``) so
every template can render a friendly label, with a graceful fall back to the
bare ISIN when the name is unknown.

Source of truth is the per-host market-data sidecar's ``/instruments`` (the full
ALL21 market list, cached ~6h on the sidecar), **supplemented** by our own
``broker_orders.symbol_title`` for ISINs the sidecar lacks (and as a resilient
source when the sidecar is briefly unreachable at warm time).

Design mirrors :mod:`app.services.brokers.registry` (module-level cache, warm at
startup, sync lookup) with two differences forced by this use-case:

* :func:`lookup` returns ``None`` (never raises) on a cold/unknown cache — the
  template just shows the bare ISIN. (registry raises because an unknown broker
  is a real error; an unknown ISIN is not.)
* a **TTL** drives re-warming, because the source is remote (the sidecar), not a
  table refreshed on CRUD mutations.

Templates are sync, so resolution in a template is a sync dict hit
(:func:`lookup` / :func:`render_symbol_label`); any (async) refresh happens in
the app's startup lifespan and at the top of the ISIN-grid routes via
:func:`ensure_instruments`.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from markupsafe import Markup, escape
from sqlalchemy import select

logger = logging.getLogger(__name__)

# None == never loaded. A dict (possibly empty) == loaded.
_CACHE: Optional[dict[str, dict]] = None
_LOADED_AT: float = 0.0

_TTL_SECONDS = 6 * 3600          # a populated cache refreshes every 6h
_EMPTY_RETRY_SECONDS = 300       # an empty/failed cache retries sooner (recover fast)


def set_instruments_map(mapping: dict[str, dict]) -> None:
    """Replace the cache directly (used by tests / seed scripts)."""
    global _CACHE, _LOADED_AT
    _CACHE = {k: dict(v) for k, v in mapping.items()}
    _LOADED_AT = time.monotonic()


def _reset() -> None:
    """Drop the cache back to the cold state (test isolation)."""
    global _CACHE, _LOADED_AT
    _CACHE = None
    _LOADED_AT = 0.0


def _is_loaded() -> bool:
    return _CACHE is not None


def _is_stale() -> bool:
    age = time.monotonic() - _LOADED_AT
    # An empty cache (warm failed, or genuinely empty) retries sooner so a
    # transient startup-time sidecar outage recovers within minutes, not 6h.
    if not _CACHE:
        return age > _EMPTY_RETRY_SECONDS
    return age > _TTL_SECONDS


async def warm_instruments(db=None) -> dict[str, dict]:
    """(Re)load the ``{isin: {"symbol", "name"}}`` map. Never raises.

    Sidecar ``/instruments`` is authoritative; ``broker_orders`` only fills the
    ISINs the sidecar didn't cover. On a hard failure the previous map is kept
    (or ``{}`` on the very first load).
    """
    global _CACHE, _LOADED_AT
    from app.models.broker_orders import BrokerOrder
    from app.services import market_data_client

    async def _load(session) -> dict[str, dict]:
        merged: dict[str, dict] = {}
        # 1) Sidecar full-market list (authoritative, fresh). get_instruments is
        #    itself graceful (returns [] on any sidecar failure), so a sidecar
        #    outage does not raise here — it just yields the supplement below.
        for it in await market_data_client.get_instruments(session):
            isin = (it.get("isin") or "").strip()
            if isin:
                merged[isin] = {
                    "symbol": (it.get("symbol") or "").strip(),
                    "name": (it.get("name") or "").strip(),
                }
        # 2) Supplement from our own executed-order history for gaps.
        rows = (
            await session.execute(
                select(BrokerOrder.isin, BrokerOrder.symbol, BrokerOrder.symbol_title)
                .where(BrokerOrder.symbol_title.isnot(None))
                .distinct()
            )
        ).all()
        for isin, symbol, title in rows:
            isin = (isin or "").strip()
            if isin and isin not in merged:
                merged[isin] = {
                    "symbol": (symbol or "").strip(),
                    "name": (title or "").strip(),
                }
        return merged

    try:
        if db is not None:
            loaded = await _load(db)
        else:
            from app.db import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                loaded = await _load(session)
    except Exception:  # noqa: BLE001 — a resolver miss must never break a page
        logger.exception("failed to (re)load instrument cache; keeping previous")
        if _CACHE is None:
            _CACHE = {}
        _LOADED_AT = time.monotonic()
        return _CACHE

    # Replace on any successful load (incl. an empty result on first warm).
    if loaded or _CACHE is None:
        _CACHE = loaded
    _LOADED_AT = time.monotonic()
    logger.info("instrument cache warmed: %d isins", len(_CACHE))
    return _CACHE


async def ensure_instruments(db=None) -> dict[str, dict]:
    """Warm on first use; re-warm if stale. Cheap no-op when warm + fresh."""
    if not _is_loaded() or _is_stale():
        await warm_instruments(db)
    assert _CACHE is not None
    return _CACHE


def lookup(isin: str) -> Optional[dict]:
    """SYNC ``{"symbol", "name"}`` for ``isin``, or ``None``.

    Returns ``None`` for an unknown ISIN or a cache that was never warmed — the
    template then shows the bare ISIN. Never raises.
    """
    if not _CACHE or not isin:
        return None
    return _CACHE.get(isin)


def symbol_text(isin: str, symbol: Optional[str] = None, title: Optional[str] = None) -> str:
    """Resolve an ISIN to a plain label string (no HTML), or ``""`` if unknown.

    Resolution order (so rows that already carry a symbol never pay a lookup and
    never regress): caller ``symbol`` → caller ``title`` → cached symbol →
    cached name → ``""``. For inline/header contexts; registered as the Jinja
    global ``symbol_text`` (e.g. ``{{ symbol_text(x.isin, symbol=x.symbol) or x.isin }}``).
    """
    label = (symbol or "").strip() or (title or "").strip()
    if not label:
        hit = lookup(isin or "")
        if hit:
            label = (hit.get("symbol") or "").strip() or (hit.get("name") or "").strip()
    return label


def render_symbol_label(isin: str, symbol: Optional[str] = None, title: Optional[str] = None) -> Markup:
    """Render a grid cell: symbol prominent (RTL) + the ISIN muted below.

    Resolution via :func:`symbol_text`; bare ISIN when unknown. Registered as the
    Jinja global ``symbol_label``.
    """
    isin = isin or ""
    label = symbol_text(isin, symbol, title)
    esc_isin = escape(isin)
    if label:
        return Markup(
            f'<code class="text-small" dir="auto">{escape(label)}</code>'
            f'<div class="text-small text-muted"><code>{esc_isin}</code></div>'
        )
    return Markup(f'<code class="text-small">{esc_isin}</code>')


__all__ = [
    "warm_instruments",
    "ensure_instruments",
    "lookup",
    "render_symbol_label",
    "symbol_text",
    "set_instruments_map",
]
