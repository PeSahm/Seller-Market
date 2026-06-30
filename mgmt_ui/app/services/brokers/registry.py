"""Broker-family resolver + adapter factory.

The ``brokers`` table is the source of truth for "which family is this code".
We keep a tiny process-cached ``{code: family}`` map (≈15 rows) so the hot
request/worker paths don't re-query Postgres per call. It's warmed at app
startup and refreshed whenever a broker is created/updated/toggled.

Lookup contract:

* :func:`family_of` is sync and reads the warm cache (raises
  :class:`UnknownBrokerError` on an unknown / not-yet-warmed code).
* the async dispatchers in ``broker_client`` call :func:`ensure_family_cache`
  first (they're async), so by the time :func:`family_of` /
  :func:`get_adapter` run the cache is guaranteed warm.
* tests seed the cache directly via :func:`set_family_map`.

The adapter classes are imported lazily inside :func:`get_adapter` so this
module (and the package ``__init__``) import cleanly even before the
``ephoenix``/``exir`` adapter modules exist.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)


class UnknownBrokerError(KeyError):
    """Raised when a broker code has no row in the ``brokers`` table."""


# None == never loaded. A dict (possibly empty) == loaded.
_FAMILY_CACHE: Optional[dict[str, str]] = None
# {code: base_domain or None} — the per-broker OnlinePlus tenant host, warmed
# alongside the family cache. Empty until the first warm.
_BASE_DOMAIN_CACHE: dict[str, Optional[str]] = {}


def set_family_map(mapping: dict[str, str]) -> None:
    """Replace the family cache directly (used by tests / seed scripts)."""
    global _FAMILY_CACHE
    _FAMILY_CACHE = dict(mapping)


def set_base_domain_map(mapping: dict[str, Optional[str]]) -> None:
    """Replace the base-domain cache directly (used by tests / seed scripts)."""
    global _BASE_DOMAIN_CACHE
    _BASE_DOMAIN_CACHE = dict(mapping)


def _is_loaded() -> bool:
    return _FAMILY_CACHE is not None


async def warm_family_cache(db=None) -> dict[str, str]:
    """(Re)load ``{code: family}`` from the ``brokers`` table.

    If ``db`` is None a short-lived session is opened. Safe to call repeatedly
    (e.g. on every broker CRUD mutation) — it fully replaces the cache.
    """
    global _FAMILY_CACHE, _BASE_DOMAIN_CACHE
    from app.models.brokers import Broker  # local import avoids cycle at import time

    async def _load(session) -> dict[str, str]:
        global _BASE_DOMAIN_CACHE
        rows = (
            await session.execute(
                select(Broker.code, Broker.family, Broker.base_domain)
            )
        ).all()
        _BASE_DOMAIN_CACHE = {code: (bd or None) for code, _family, bd in rows}
        return {code: family for code, family, _bd in rows}

    if db is not None:
        _FAMILY_CACHE = await _load(db)
    else:
        from app.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            _FAMILY_CACHE = await _load(session)
    logger.info("broker family cache warmed: %d brokers", len(_FAMILY_CACHE))
    return _FAMILY_CACHE


async def ensure_family_cache(db=None) -> dict[str, str]:
    """Warm the cache once if it has never been loaded; otherwise no-op."""
    if not _is_loaded():
        await warm_family_cache(db)
    assert _FAMILY_CACHE is not None
    return _FAMILY_CACHE


def family_of(code: str) -> str:
    """Return the family for a broker code from the warm cache.

    Raises :class:`UnknownBrokerError` if the code is unknown or the cache was
    never warmed. Callers on async paths should ``await ensure_family_cache()``
    first.
    """
    if _FAMILY_CACHE is None:
        raise UnknownBrokerError(
            f"broker family cache not warmed (looking up {code!r}); "
            "call await ensure_family_cache() first"
        )
    try:
        return _FAMILY_CACHE[code]
    except KeyError as exc:
        raise UnknownBrokerError(f"unknown broker code: {code!r}") from exc


def base_domain_of(code: str) -> Optional[str]:
    """Return the per-broker ``base_domain`` (OnlinePlus tenant host) from the
    warm cache, or ``None`` (unknown code / not set / cache not warmed). Unlike
    :func:`family_of` this never raises — a missing base_domain just means the
    OnlinePlus adapter falls back to the ``{code}broker.ir`` convention."""
    return _BASE_DOMAIN_CACHE.get(code)


def get_adapter(code: str):
    """Resolve a broker code to a concrete adapter instance (sync; cache-warm).

    Lazy-imports the adapter classes so the package imports before the adapter
    modules are written / to avoid import cycles.
    """
    family = family_of(code)
    if family == "ephoenix":
        from app.services.brokers.ephoenix import EphoenixAdapter

        return EphoenixAdapter(code)
    if family == "exir":
        from app.services.brokers.exir import ExirAdapter

        return ExirAdapter(code)
    if family == "onlineplus":
        from app.services.brokers.onlineplus import OnlinePlusAdapter

        return OnlinePlusAdapter(code)
    if family == "mofid":
        from app.services.brokers.mofid import MofidAdapter

        return MofidAdapter(code)
    raise UnknownBrokerError(f"no adapter for family {family!r} (code {code!r})")
