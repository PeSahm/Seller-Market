"""Per-stack locust config upsert orchestration (Phase 5).

Every :class:`app.models.scheduler.LocustConfig` row holds the inputs a
single agent stack hands to ``locust`` when the bot triggers a load run.
There's at most one row per stack (enforced by a UNIQUE constraint on
``stack_id``) so the only mutation the router needs is a create-or-update
"upsert".

What lives here (vs. the schema)
--------------------------------
The static field shapes — value ranges, run_time format, host scheme — live
in :mod:`app.schemas.locust`. This module is responsible for two things
pydantic can't do on its own:

1. **Dynamic ``processes`` cap.** The cap is admin-tunable via the
   ``agent_locust_processes_cap`` setting (default ``4``). The pydantic
   schema only enforces a static ceiling of ``32`` — the dynamic floor /
   ceiling check has to run at request time against the DB, which is what
   :func:`upsert_locust_config` does.
2. **Optimistic locking.** Two admins racing on the same row must not
   silently overwrite each other. The caller echoes the ``version`` they
   read; a mismatch raises :class:`OptimisticLockError` (router → HTTP 409).

Audit
-----
Every successful upsert writes an ``audit_log`` row with
``action="locust.update"`` and ``target_type="locust_config"``. ``before``
is the previous row snapshot (or ``None`` on first-time insert); ``after``
is the freshly-applied row. No secret material lives on this model, so we
serialise every column directly.
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.scheduler import LocustConfig
from app.schemas.locust import LocustUpsert
from app.services import settings_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OptimisticLockError(Exception):
    """Raised when the caller's ``version`` doesn't match the row's current.

    The router catches this and returns HTTP 409 with a reload-and-retry
    message — same pattern as
    :class:`app.services.customers.OptimisticLockError`. A typed exception
    (rather than a generic ``ValueError``) keeps the router's error mapping
    explicit and lets future callers distinguish the lock failure from
    other validation problems.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _public_snapshot(row: LocustConfig) -> dict:
    """Audit-safe dict of a LocustConfig row.

    No secret material lives on this model, so every column is included.
    Kept as a helper (instead of a one-line dict at call sites) so adding
    a new column later only requires editing one place — same convention
    as :func:`app.services.customers._public_snapshot`.
    """
    return {
        "id": str(row.id),
        "stack_id": str(row.stack_id),
        "users": row.users,
        "spawn_rate": row.spawn_rate,
        "run_time": row.run_time,
        "host": row.host,
        "processes": row.processes,
        "version": row.version,
    }


async def _write_audit(
    db: AsyncSession,
    *,
    actor_id: Optional[UUID],
    target_id: UUID,
    before: Optional[dict],
    after: dict,
) -> None:
    """Insert a single ``audit_log`` row for a locust upsert.

    ``target_type`` is always ``"locust_config"`` and ``action`` is always
    ``"locust.update"`` (whether the row was just inserted or updated — the
    distinction is captured by ``before is None``).
    """
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action="locust.update",
            target_type="locust_config",
            target_id=str(target_id),
            before_json=before,
            after_json=after,
        )
    )


async def _resolve_processes_cap(db: AsyncSession) -> int:
    """Read the admin-set ``processes`` cap from the settings store.

    Falls back to the documented default (``4``) when the setting hasn't
    been touched by an admin yet. A non-integer DB value is a programmer
    error (the settings form validates the cap as ``int``); if one ever
    sneaks in, we let ``int()`` raise — better to fail loudly than to
    silently dispatch with an unbounded ``processes`` count.
    """
    raw = await settings_store.get_setting(db, "agent_locust_processes_cap")
    return int(raw)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def get_locust_config(
    db: AsyncSession,
    stack_id: UUID,
) -> Optional[LocustConfig]:
    """Return the (single) locust config row for ``stack_id``, or ``None``.

    The UNIQUE constraint on ``stack_id`` guarantees at most one row — we
    use ``scalar_one_or_none`` rather than ``.first()`` so a future bug
    that violated the constraint would surface as a loud
    ``MultipleResultsFound`` here, not silent data loss.
    """
    stmt = select(LocustConfig).where(LocustConfig.stack_id == stack_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


async def upsert_locust_config(
    db: AsyncSession,
    stack_id: UUID,
    data: LocustUpsert,
    actor_id: UUID,
) -> LocustConfig:
    """Create-or-update the locust config row for ``stack_id``.

    Behaviour
    ---------
    * **Insert** when no row exists. Caller passes ``data.version=0``; we
      ignore the value on the insert path (the row starts at ``version=1``
      via the column default — we set it explicitly so SQLite-backed tests
      that don't honour ``server_default`` see the same value).
    * **Update** when a row exists. Caller's ``version`` MUST match the
      row's current ``version``; mismatch raises :class:`OptimisticLockError`
      and nothing is written. On success the new ``version`` is
      ``old + 1``.

    Dynamic cap
    -----------
    ``data.processes`` is checked against the admin-set
    ``agent_locust_processes_cap`` (default 4). A value exceeding the cap
    raises ``ValueError`` with a message naming the cap — this check is
    here (NOT in the pydantic schema) because the cap is dynamic and
    pydantic field validators have no DB access.

    Race conditions
    ---------------
    Two admins inserting concurrently could both miss the "no row exists"
    check and both attempt an insert; the UNIQUE constraint on
    ``stack_id`` means one of them gets an :class:`IntegrityError`. We
    catch that, rollback, and re-read the row — the second writer then
    transparently becomes the "update" path. The caller's ``version=0``
    would now mismatch (the row already has ``version=1`` from the first
    writer), so they correctly get :class:`OptimisticLockError` and can
    refresh + retry.
    """
    cap = await _resolve_processes_cap(db)
    if data.processes > cap:
        raise ValueError(
            f"processes exceeds the per-stack cap of {cap}"
        )

    existing = await get_locust_config(db, stack_id)

    if existing is None:
        # Insert path. We try once; if a concurrent insert wins the race
        # we rollback, re-read, and fall through to the update branch.
        row = LocustConfig(
            stack_id=stack_id,
            users=data.users,
            spawn_rate=data.spawn_rate,
            run_time=data.run_time,
            host=data.host,
            processes=data.processes,
            version=1,
        )
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            # Re-fetch — another writer just won. From this caller's POV
            # the row now "exists with a version they didn't supply", so
            # surface the standard optimistic-lock error.
            other = await get_locust_config(db, stack_id)
            if other is None:
                # Truly weird — the constraint fired but no row is
                # visible. Re-raise rather than fall through to a bogus
                # update.
                raise
            raise OptimisticLockError(
                f"version mismatch: row has {other.version}, "
                f"caller had {data.version}"
            )

        await _write_audit(
            db,
            actor_id=actor_id,
            target_id=row.id,
            before=None,
            after=_public_snapshot(row),
        )
        await db.commit()
        await db.refresh(row)
        return row

    # Update path. Optimistic-lock check first — refuse to mutate on
    # mismatch and DO NOT write an audit row (the operation didn't
    # happen). Matches the convention in
    # :func:`app.services.customers.update_customer`.
    if existing.version != data.version:
        raise OptimisticLockError(
            f"version mismatch: row has {existing.version}, "
            f"caller had {data.version}"
        )

    before = _public_snapshot(existing)
    existing.users = data.users
    existing.spawn_rate = data.spawn_rate
    existing.run_time = data.run_time
    existing.host = data.host
    existing.processes = data.processes
    existing.version += 1

    await db.flush()
    await _write_audit(
        db,
        actor_id=actor_id,
        target_id=existing.id,
        before=before,
        after=_public_snapshot(existing),
    )
    await db.commit()
    await db.refresh(existing)
    return existing


__all__ = [
    "OptimisticLockError",
    "get_locust_config",
    "upsert_locust_config",
]
