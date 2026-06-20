from __future__ import annotations

import hashlib
import logging
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.settings import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

# ---------------------------------------------------------------------------
# App-level DB failover (#156).
#
# ``AsyncSessionLocal`` is imported at module top in 20+ modules (workers,
# services, routers, leader election). Swapping a *global* would not reach
# those already-bound references — so failover instead REBINDS this one shared
# sessionmaker's engine IN PLACE via ``.configure(bind=...)``. Because every
# importer shares the same object, they all see the new engine on their next
# ``AsyncSessionLocal()`` call (verified: same object identity, ``.begin()``
# preserved). ``engine`` (the main) is never reassigned, so the failover
# supervisor can keep probing it to detect when the main comes back.
# ---------------------------------------------------------------------------

_active_db = "main"
spare_engine = None  # built lazily on first failover


def _build_engine(dsn: str):
    return create_async_engine(dsn, pool_pre_ping=True, future=True)


def active_db() -> str:
    """Which database the shared sessionmaker is currently bound to.

    ``"main"`` (the configured ``DATABASE_URL``) or ``"spare"`` (the warm
    standby) after a failover.
    """
    return _active_db


def get_spare_engine():
    """Return the spare engine (built lazily from ``spare_dsn``), or ``None``
    when no spare is configured."""
    global spare_engine
    if spare_engine is None:
        dsn = get_settings().spare_dsn
        if not dsn:
            return None
        spare_engine = _build_engine(dsn)
    return spare_engine


async def activate_spare() -> bool:
    """Rebind the shared sessionmaker to the warm spare. Idempotent.

    Returns ``True`` once bound to the spare, ``False`` if no spare DSN is
    configured (caller stays on the dead main → 503s). Does NOT auto-fail-back
    — returning to the main is a deliberate restart after a resync, to avoid
    split-brain writes against two diverging databases.
    """
    global _active_db
    eng = get_spare_engine()
    if eng is None:
        logger.error("DB failover requested but no spare_dsn configured — staying on main")
        return False
    AsyncSessionLocal.configure(bind=eng)
    _active_db = "spare"
    logger.warning("DB FAILOVER: shared sessionmaker rebound to the SPARE database")
    return True


def _reset_to_main_for_tests() -> None:
    """Test-only: restore the sessionmaker to the main engine + clear state."""
    global _active_db, spare_engine
    AsyncSessionLocal.configure(bind=engine)
    _active_db = "main"
    spare_engine = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an AsyncSession."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def acquire_advisory_lock(
    session: AsyncSession,
    key: int,
    transaction_scoped: bool = True,
) -> bool:
    """Try to acquire a PostgreSQL advisory lock.

    Uses pg_try_advisory_xact_lock (transaction-scoped) when
    ``transaction_scoped`` is True, else pg_try_advisory_lock.

    Returns True if the lock was acquired, False otherwise.
    """
    if transaction_scoped:
        stmt = text("SELECT pg_try_advisory_xact_lock(:key)")
    else:
        stmt = text("SELECT pg_try_advisory_lock(:key)")
    result = await session.execute(stmt, {"key": key})
    acquired = bool(result.scalar())
    return acquired


async def try_acquire_session_lock(session: AsyncSession, key: int) -> bool:
    """Try to acquire a *session-scoped* PostgreSQL advisory lock.

    Unlike :func:`acquire_advisory_lock` (which by default uses the
    transaction-scoped variant and is released on ``COMMIT`` / ``ROLLBACK``),
    a session-scoped lock survives commits and stays held until either the
    connection closes or :func:`release_session_lock` is called.

    This is what callers want when they need to hold a lock across multiple
    transactions on the *same* asyncpg connection (e.g. mark a row as
    ``in-progress`` and commit, then do remote SSH work, then commit a final
    status update — all under one continuous lock).

    Returns True if the lock was acquired, False otherwise.
    """
    stmt = text("SELECT pg_try_advisory_lock(:key)")
    result = await session.execute(stmt, {"key": key})
    return bool(result.scalar())


async def release_session_lock(session: AsyncSession, key: int) -> None:
    """Release a session-scoped advisory lock previously acquired on ``session``.

    Pairs with :func:`try_acquire_session_lock`. Must be called on the same
    session (= same underlying connection) that acquired the lock — otherwise
    Postgres returns ``false`` (a no-op release) and the lock leaks until the
    holding connection closes.
    """
    await session.execute(
        text("SELECT pg_advisory_unlock(:key)"), {"key": key}
    )


def hash_lock_key(*parts: str | int) -> int:
    """Produce a stable signed 64-bit integer suitable for pg advisory locks.

    Uses BLAKE2b with an 8-byte digest for cross-version stability
    (unlike Python's built-in hash() which is randomized per process).

    Each part is length-prefixed (4-byte big-endian length followed by its
    UTF-8 bytes) before being fed into the hash. This prevents collisions
    between different ``parts`` tuples that would otherwise serialize to the
    same byte stream (e.g. ``("a|b",)`` vs ``("a", "b")``), so unrelated
    resources cannot contend on the same advisory-lock key.
    """
    h = hashlib.blake2b(digest_size=8)
    for part in parts:
        b = str(part).encode("utf-8")
        h.update(len(b).to_bytes(4, "big"))
        h.update(b)
    digest = h.digest()
    return int.from_bytes(digest, "big", signed=True)
