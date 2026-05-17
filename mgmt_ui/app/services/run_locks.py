"""Stack-level mutex with TTL for in-flight runs (Phase 6).

Each agent stack can have at most one of ``cache_warmup`` / ``run_trading``
executing at a time — running two in parallel would race on the broker's
session cache (cache_warmup) or double-place orders (run_trading). The
:class:`app.models.runs.StackRunLock` row is the DURABLE ledger of "what's
running on this stack right now"; the Postgres advisory-lock helpers in
:mod:`app.db` are the SERIALIZATION primitive that protects the
SELECT-then-INSERT inside :func:`acquire_lock` from a TOCTOU race between
two competing manual-run requests.

Lease + reclaim
---------------
Every lock carries a ``lease_expires_at``. If the mgmt process holding
the lock crashes (OOM, container restart, kill -9), the row is left
pinned with no live holder. The next call to :func:`acquire_lock` sees
the stale row, logs a WARNING, deletes it, and proceeds. The lease
window (default 600 s, matching the bot's subprocess timeout in
``SellerMarket/scheduler.py:227``) is deliberately short for manual
runs and can be extended by long-running locust runs via
:func:`extend_lease`.

Release safety
--------------
:func:`release_lock` deletes by ``(stack_id, run_id)`` BOTH, never by
``stack_id`` alone. This means a slow release that happens AFTER another
holder has reclaimed the stale lock will no-op instead of stealing the
newcomer's lock.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runs import StackRunLock

logger = logging.getLogger(__name__)

# Manual runs are capped at 10 minutes. This matches the existing bot
# scheduler's subprocess timeout in SellerMarket/scheduler.py:227 so a
# manual invocation can't outlive the equivalent scheduled one.
DEFAULT_LEASE_SECONDS = 600


class StackRunLockBusyError(RuntimeError):
    """Raised by :func:`acquire_lock` when a non-stale row already exists.

    The route catches this and turns it into HTTP 409 with the holder
    string and lease expiry so the operator can decide whether to wait
    or kill the in-flight run.
    """

    def __init__(self, stack_id: UUID, holder: str, expires_at: datetime):
        self.stack_id = stack_id
        self.holder = holder
        self.expires_at = expires_at
        super().__init__(
            f"stack {stack_id} is busy (held by {holder!r} until "
            f"{expires_at.isoformat()})"
        )


async def acquire_lock(
    db: AsyncSession,
    *,
    stack_id: UUID,
    run_id: UUID,
    kind: str,
    holder: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> StackRunLock:
    """Insert a ``stack_run_locks`` row, reclaiming a stale one if present.

    The SELECT uses ``FOR UPDATE`` so a second concurrent caller blocks on
    the row lock rather than racing past our existence check. If no row
    exists yet, the INSERT serializes against the table's PRIMARY KEY
    constraint and one of the two callers will see a ``UniqueViolation``
    — but that's a vanishingly rare path because the route layer wraps
    this in a per-stack advisory lock (see :mod:`app.db`) before calling.

    Args:
        stack_id: Target stack — also the lock row's PK.
        run_id: The :class:`app.models.runs.Run` row this lock is bound
            to. Stored on the lock so :func:`release_lock` can verify
            "this is my lock" before deleting.
        kind: ``"cache"`` for cache_warmup, ``"trade"`` for run_trading.
            The DB enforces this via the ``stack_run_lock_kind`` enum.
        holder: Informational free-text — e.g. ``"manual:<user_id>"`` or
            ``"scheduled"``. Surfaced in the 409 error so the operator
            knows who currently holds it.
        lease_seconds: How long until the lock is considered stale and
            reclaimable. Default 600 s.

    Raises:
        StackRunLockBusyError: A live (non-stale) lock already exists.
    """
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=lease_seconds)

    # SELECT FOR UPDATE the existing row (if any) — blocks any other
    # caller in the same transaction window from racing past us. Postgres
    # treats FOR UPDATE on a missing row as a no-op (it returns no row,
    # no lock taken); the subsequent INSERT serialises on the PK instead.
    existing = await db.execute(
        select(StackRunLock)
        .where(StackRunLock.stack_id == stack_id)
        .with_for_update()
    )
    row = existing.scalar_one_or_none()

    if row is not None:
        if row.lease_expires_at > now:
            raise StackRunLockBusyError(
                stack_id, row.holder, row.lease_expires_at
            )
        # Stale row — the previous holder crashed before releasing.
        # WARNING-level log so it shows up in alerting; we don't want
        # silent reclaims to mask a recurring crash pattern.
        logger.warning(
            "reclaiming stale stack_run_lock stack=%s prior_holder=%s "
            "prior_expires=%s",
            stack_id,
            row.holder,
            row.lease_expires_at.isoformat(),
        )
        await db.execute(
            delete(StackRunLock).where(StackRunLock.stack_id == stack_id)
        )
        await db.flush()

    lock = StackRunLock(
        stack_id=stack_id,
        run_id=run_id,
        kind=kind,
        started_at=now,
        lease_expires_at=expires,
        holder=holder,
    )
    db.add(lock)
    await db.flush()
    return lock


async def release_lock(
    db: AsyncSession,
    *,
    stack_id: UUID,
    run_id: UUID,
) -> None:
    """Delete the lock row, but only if both keys match.

    Filtering by ``run_id`` as well as ``stack_id`` is the safety net for
    the "slow release after reclaim" race: if our run hung past the
    lease, another caller already deleted our row and inserted theirs.
    A naive ``DELETE WHERE stack_id = :s`` would then drop the new
    holder's lock — silently corrupting the mutex. With the compound
    WHERE the slow release is a harmless no-op.
    """
    await db.execute(
        delete(StackRunLock).where(
            StackRunLock.stack_id == stack_id,
            StackRunLock.run_id == run_id,
        )
    )


async def extend_lease(
    db: AsyncSession,
    *,
    stack_id: UUID,
    run_id: UUID,
    seconds: int,
) -> None:
    """Bump ``lease_expires_at`` for a held lock.

    Used by long-running locust runs that legitimately exceed the
    default 600-second lease. Idempotent: callers can extend on a timer
    without checking whether the lease is already in the future. The
    compound ``(stack_id, run_id)`` WHERE means an extend issued after
    the lock has already been reclaimed is a silent no-op rather than a
    cross-tenant lease bump.
    """
    new_expires = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    await db.execute(
        StackRunLock.__table__
        .update()
        .where(StackRunLock.stack_id == stack_id)
        .where(StackRunLock.run_id == run_id)
        .values(lease_expires_at=new_expires)
    )


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "StackRunLockBusyError",
    "acquire_lock",
    "release_lock",
    "extend_lease",
]
