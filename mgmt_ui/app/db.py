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


def hash_lock_key(*parts: str | int) -> int:
    """Produce a stable signed 64-bit integer suitable for pg advisory locks.

    Uses BLAKE2b with an 8-byte digest for cross-version stability
    (unlike Python's built-in hash() which is randomized per process).
    """
    s = "|".join(map(str, parts))
    digest = hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)
