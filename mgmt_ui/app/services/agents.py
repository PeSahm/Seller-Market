"""Agent CRUD orchestration (Phase 3).

Agents are :class:`~app.models.users.User` rows with ``role='agent'``. This
module is the seam between the admin HTTP routes and the lower-level pieces:

* :mod:`app.models.users` for the DB row
* :mod:`app.security.auth` for bcrypt password hashing
* :mod:`app.models.audit` for write-side audit logging

The router stays thin: it converts a form into a pydantic
:class:`~app.schemas.agent.AgentCreate`, calls one function here, and renders
the result.

Phase 3 scope
-------------
We deliberately do NOT touch agent-owned downstream rows (customers, stacks,
runs) on soft-delete. The plan calls for orphan cleanup in Phase 4 / Phase 9
once those tables exist and we have a UX for re-assignment. For now,
soft-delete simply flips ``deleted_at`` so the agent disappears from the list
and can no longer log in (the :func:`~app.security.deps._load_user` check
already rejects soft-deleted users).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.users import User
from app.schemas.agent import AgentCreate
from app.security.auth import hash_password

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    """A timezone-aware UTC ``datetime`` for TIMESTAMPTZ columns."""
    return datetime.now(timezone.utc)


def _public_snapshot(agent: User) -> dict:
    """Audit-safe dict of an agent User row.

    Critically excludes ``password_hash`` — the bcrypt digest stays out of
    the audit log so a stolen audit dump can't be brute-forced offline. The
    role is fixed at ``'agent'`` for everything this module produces, so we
    omit it as well to keep payloads small.
    """
    return {
        "id": str(agent.id),
        "username": agent.username,
        "telegram_user_id": agent.telegram_user_id,
        "deleted_at": agent.deleted_at.isoformat() if agent.deleted_at else None,
    }


async def _write_audit(
    db: AsyncSession,
    *,
    actor_id: Optional[UUID],
    action: str,
    target_id: UUID,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
) -> None:
    """Insert a single ``audit_log`` row.

    ``target_type`` is always ``"user"`` for this module (agents are users).
    The caller is responsible for ensuring ``before`` / ``after`` contain no
    secret material — in particular, never put ``password_hash`` in either
    payload. :func:`_public_snapshot` enforces this for the happy path.
    """
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action=action,
            target_type="user",
            target_id=str(target_id),
            before_json=before,
            after_json=after,
        )
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_agents(
    db: AsyncSession,
    *,
    include_deleted: bool = False,
) -> list[User]:
    """Return all agents ordered by username.

    By default soft-deleted rows are hidden. Pass ``include_deleted=True`` to
    surface them — useful for an "all agents (incl. archived)" view later;
    today the only caller is the admin list page which never sets it.
    """
    stmt = select(User).where(User.role == "agent")
    if not include_deleted:
        stmt = stmt.where(User.deleted_at.is_(None))
    stmt = stmt.order_by(User.username)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_agent(db: AsyncSession, agent_id: UUID) -> Optional[User]:
    """Look up a single agent by id.

    Returns the User row regardless of ``role`` — the router checks the role
    afterwards and 404s if it's not an agent, so an admin accidentally hitting
    ``/admin/agents/<admin-user-id>`` gets a clean 404 instead of leaking the
    existence of another admin via this endpoint.
    """
    stmt = select(User).where(User.id == agent_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def create_agent(
    db: AsyncSession,
    data: AgentCreate,
    actor_id: UUID,
) -> User:
    """Insert a new agent and write the create audit entry.

    Raises ``ValueError("username already taken")`` if the username is in use.
    The check is case-sensitive — usernames are stored verbatim and a future
    case-insensitive policy would have to backfill existing rows, so we don't
    pretend to be case-insensitive today.

    Order of operations:

    1. Check the username is free.
    2. Hash the plaintext password with bcrypt.
    3. Insert the User row with ``role='agent'`` and ``deleted_at=None``.
    4. Write the audit log entry (payload from :func:`_public_snapshot`, so
       no password material).
    5. Commit + refresh.

    The plaintext password is never logged and never stored — it lives only
    inside the bcrypt hashing call and is dropped when this function returns.
    """
    # Step 1: uniqueness check. We do it explicitly rather than rely on the
    # DB unique-constraint exception because we want a clean ValueError the
    # router can translate to a friendly form error.
    existing_stmt = select(User.id).where(User.username == data.username)
    existing = await db.execute(existing_stmt)
    if existing.scalar_one_or_none() is not None:
        raise ValueError("username already taken")

    # Step 2: hash. bcrypt is slow on purpose; we do this on the request
    # thread because the rest of this function is fast and we'd rather keep
    # the operation atomic than spawn a thread for ~200ms of work.
    password_hash = hash_password(data.password)

    # Step 3: insert.
    agent = User(
        username=data.username,
        password_hash=password_hash,
        role="agent",
        telegram_user_id=data.telegram_user_id,
        deleted_at=None,
    )
    db.add(agent)
    # Flush so the DB-side default (gen_random_uuid) populates agent.id
    # before we reference it in the audit row.
    await db.flush()

    # Step 4: audit. _public_snapshot excludes password_hash by construction.
    await _write_audit(
        db,
        actor_id=actor_id,
        action="agent.create",
        target_id=agent.id,
        before=None,
        after=_public_snapshot(agent),
    )

    # Step 5: commit + refresh.
    await db.commit()
    await db.refresh(agent)
    return agent


# ---------------------------------------------------------------------------
# Delete (soft)
# ---------------------------------------------------------------------------


async def soft_delete_agent(
    db: AsyncSession,
    agent_id: UUID,
    actor_id: UUID,
) -> None:
    """Soft-delete an agent by stamping ``deleted_at = now()``.

    Idempotent — calling this on an unknown id or an already-deleted agent is
    a no-op. We do NOT delete or reassign the agent's customers / stacks /
    runs; Phase 4 owns that cleanup once those rows exist.

    A soft-deleted agent is immediately blocked from logging in because
    :func:`~app.security.deps._load_user` treats any ``deleted_at IS NOT NULL``
    user as missing.
    """
    agent = await get_agent(db, agent_id)
    if agent is None:
        # Idempotent: deleting a missing agent is fine.
        return
    if agent.role != "agent":
        # Defense in depth: refuse to soft-delete an admin via this code path.
        # The router 404s on this already, but we double-check so a future
        # caller can't accidentally nuke an admin.
        raise ValueError("target is not an agent")
    if agent.deleted_at is not None:
        # Already deleted; nothing to do. We still don't write a second audit
        # row — re-deleting isn't a meaningful event.
        return

    before = _public_snapshot(agent)
    agent.deleted_at = _now_utc()

    await _write_audit(
        db,
        actor_id=actor_id,
        action="agent.delete",
        target_id=agent.id,
        before=before,
        after=_public_snapshot(agent),
    )
    await db.commit()


__all__ = [
    "create_agent",
    "get_agent",
    "list_agents",
    "soft_delete_agent",
]
