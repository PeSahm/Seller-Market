"""Customer (brokerage account) CRUD orchestration.

Post-migration 0003, ``Customer`` is account-shaped — broker credentials
plus a display name. The per-instrument fields (isin, side, comment,
section_name) moved to :class:`app.models.trade_instructions.TradeInstruction`;
see :mod:`app.services.trade_instructions` for the parallel CRUD module.

The router stays thin: it converts a form into a pydantic model, calls one
function here, and renders the result. Anything involving more than a single
SQL statement lives here.

Secret hygiene
--------------
Plaintext passwords NEVER touch the database or the audit log. They enter
this module via ``CustomerCreate.password`` / ``CustomerUpdate.password``,
get Fernet-encrypted into ``Customer.password_enc``, and are then dropped.
The :func:`_public_snapshot` helper excludes ``password_enc`` from audit
payloads by construction. The only function that decrypts is
:func:`decrypt_password`, used by the render layer; it relies on
:func:`app.security.crypto.decrypt` which emits a structured audit log
line on every call (the "secret hygiene" rule from the plan).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.customers import Customer
from app.models.trade_instructions import TradeInstruction
from app.schemas.customer import CustomerCreate, CustomerUpdate
from app.security.crypto import decrypt as fernet_decrypt
from app.security.crypto import encrypt as fernet_encrypt
from app.services import brokers_admin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OptimisticLockError(Exception):
    """Raised when an update's ``version`` doesn't match the row's current.

    The router catches this and returns HTTP 409 with a message asking the
    user to reload and retry. We keep a typed exception (instead of a generic
    ``ValueError``) because the router needs to distinguish it from the
    UNIQUE-constraint ``ValueError`` raised on duplicate (agent, broker,
    username) tuples.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    """A timezone-aware UTC ``datetime`` for TIMESTAMPTZ columns."""
    return datetime.now(timezone.utc)


def _public_snapshot(customer: Customer) -> dict:
    """Audit-safe dict of a Customer row (NO secret material).

    Critically excludes ``password_enc`` so the Fernet ciphertext never lands
    in an audit-log JSONB column where it would persist past any future
    Fernet key rotation. The render layer reads ``password_enc`` directly off
    the ORM row — it doesn't go through this dict.
    """
    return {
        "id": str(customer.id),
        "agent_id": str(customer.agent_id),
        "server_id": str(customer.server_id) if customer.server_id else None,
        "stack_id": str(customer.stack_id) if customer.stack_id else None,
        "assignment_status": customer.assignment_status,
        "display_name": customer.display_name,
        "username": customer.username,
        "broker": customer.broker,
        "version": customer.version,
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

    ``target_type`` is always ``"customer"`` for this module. The caller is
    responsible for ensuring ``before``/``after`` contain no secret material;
    :func:`_public_snapshot` enforces that on the happy path by omitting
    ``password_enc``.
    """
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action=action,
            target_type="customer",
            target_id=str(target_id),
            before_json=before,
            after_json=after,
        )
    )


async def _validate_broker(db: AsyncSession, broker: str) -> str:
    """Pre-check a broker code against the ``brokers`` table; return its canonical code.

    Raises plain ``ValueError`` (which the routers already translate into a
    friendly flash, never a 500) if the code is unknown or disabled. This is a
    READ-ONLY pre-check run BEFORE any insert/flush, so — unlike the
    duplicate-tuple path — it deliberately does NOT ``db.rollback()`` (there's
    nothing to roll back, and a rollback would needlessly expire loaded attrs).
    The ``brokers`` table is the single source of truth now that the schema
    layer no longer pins a closed ``Literal`` of broker codes.

    Returns ``b.code`` — the CANONICAL (lowercase) code from the registry — so
    callers persist that rather than the caller's raw casing. ``brokers`` codes
    are lowercase end-to-end, which keeps exact-match routing
    (``registry.family_of(code)``) from being defeated by casing.
    """
    b = await brokers_admin.get_broker_by_code(db, broker)
    if b is None:
        raise ValueError(f"unknown broker: {broker!r}")
    if not b.enabled:
        raise ValueError(f"broker is disabled: {broker!r}")
    return b.code


def _escape_ilike(value: str) -> str:
    """Escape ILIKE wildcards (``%`` and ``_``) in user-supplied search text.

    Without this, ``?q=%`` would match everything, and ``?q=_`` would match
    any single-character display_name. We escape with backslash and tell
    Postgres to honor it via the ``ESCAPE '\\\\'`` modifier on the LIKE
    operator (set per-query via SQLAlchemy's ``.like(escape='\\\\')``).
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_customers(
    db: AsyncSession,
    *,
    agent_id: Optional[UUID] = None,
    status: Optional[str] = None,
    server_id: Optional[UUID] = None,
    broker: Optional[str] = None,
    q: Optional[str] = None,
) -> list[Customer]:
    """List customers with optional filters.

    ``q`` is a free-text search over ``display_name`` AND ``username``
    (case-insensitive, parameterized ILIKE with escaped wildcards so a
    literal ``%`` or ``_`` in the query string matches itself rather than
    everything). Empty / whitespace-only ``q`` is treated as "no filter".

    The result is ordered by ``display_name`` for a stable UX.
    """
    stmt = select(Customer)
    if agent_id is not None:
        stmt = stmt.where(Customer.agent_id == agent_id)
    if status is not None:
        stmt = stmt.where(Customer.assignment_status == status)
    if server_id is not None:
        stmt = stmt.where(Customer.server_id == server_id)
    if broker is not None:
        stmt = stmt.where(Customer.broker == broker)
    if q is not None and q.strip():
        pat = f"%{_escape_ilike(q.strip())}%"
        stmt = stmt.where(
            Customer.display_name.ilike(pat, escape="\\")
            | Customer.username.ilike(pat, escape="\\")
        )
    stmt = stmt.order_by(Customer.display_name)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_customer(db: AsyncSession, customer_id: UUID) -> Optional[Customer]:
    """Look up a single customer by id, regardless of ``enabled`` state.

    Returning soft-deleted rows is intentional: the admin UI needs to be able
    to view a disabled customer (to see audit history, or to re-enable them).
    The render layer filters disabled rows itself via ``list_customers``.
    """
    stmt = select(Customer).where(Customer.id == customer_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_customer_trade_counts(
    db: AsyncSession, customer_ids: list[UUID]
) -> dict[UUID, int]:
    """For a list of customer ids, return ``{id: total}``.

    Powers the customer-list page's "trades" column. One query for the
    whole page rather than N+1 per row. Customer ids absent from the
    result map have zero trades.
    """
    if not customer_ids:
        return {}
    stmt = (
        select(
            TradeInstruction.customer_id,
            func.count().label("total"),
        )
        .where(TradeInstruction.customer_id.in_(customer_ids))
        .group_by(TradeInstruction.customer_id)
    )
    result = await db.execute(stmt)
    return {row.customer_id: row.total for row in result.all()}


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def create_customer(
    db: AsyncSession,
    agent_id: UUID,
    data: CustomerCreate,
    actor_id: UUID,
) -> Customer:
    """Insert a new account-shaped customer owned by ``agent_id``.

    Raises ``ValueError`` if the new composite UNIQUE
    ``(agent_id, broker, username)`` fires — i.e. this agent already has a
    customer for that account on that broker. The router translates the
    ValueError to a friendly 422.

    Trade instructions are added in a separate step via
    :mod:`app.services.trade_instructions` — adding a customer no longer
    requires picking an ISIN.
    """
    # Validate the broker against the DB BEFORE inserting (read-only pre-check;
    # raises plain ValueError → friendly 422 via the router, no 500). Persist the
    # CANONICAL (lowercase) code the registry returns, not the caller's casing.
    broker_code = await _validate_broker(db, data.broker)

    customer = Customer(
        agent_id=agent_id,
        server_id=None,
        stack_id=None,
        assignment_status="pending",
        display_name=data.display_name,
        username=data.username,
        password_enc=fernet_encrypt(data.password),
        broker=broker_code,
        version=1,
    )
    db.add(customer)

    try:
        await db.flush()
        await _write_audit(
            db,
            actor_id=actor_id,
            action="customer.create",
            target_id=customer.id,
            before=None,
            after=_public_snapshot(customer),
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(
            "customer already exists for this agent / broker / account"
        ) from exc

    await db.refresh(customer)
    return customer


# ---------------------------------------------------------------------------
# Copy to another agent (admin)
# ---------------------------------------------------------------------------


async def copy_customer_to_agent(
    db: AsyncSession,
    customer_id: UUID,
    target_agent_id: UUID,
    actor_id: UUID,
) -> Customer:
    """Copy an account + all its trade instructions to another agent.

    Lets the same brokerage account serve two agents without re-keying it.
    The copy is INDEPENDENT (its own rows; later edits don't sync) and is
    left **pending / unassigned** for the operator to assign a server.

    The composite UNIQUE ``(agent_id, broker, username)`` permits the copy
    because the target agent differs from the source. Raises:
      * ``LookupError`` — source customer missing.
      * ``ValueError`` — target is the same agent, the target agent doesn't
        exist (or is soft-deleted), or the target already has this account
        (broker/username) — the duplicate UNIQUE fires.
    """
    # Lazy import to keep the shared section-name builder the single source of
    # truth without risking an import-time cycle between the two services.
    from app.models.users import User
    from app.services.trade_instructions import _build_section_name

    source = await get_customer(db, customer_id)
    if source is None:
        raise LookupError(f"customer {customer_id} not found")
    if target_agent_id == source.agent_id:
        raise ValueError("customer already belongs to that agent")

    target = await db.get(User, target_agent_id)
    if target is None or target.deleted_at is not None:
        raise ValueError("target agent not found")

    # Snapshot the source's trade instructions BEFORE adding the copy.
    source_tis = list(
        (
            await db.execute(
                select(TradeInstruction).where(
                    TradeInstruction.customer_id == source.id
                )
            )
        )
        .scalars()
        .all()
    )

    new_customer = Customer(
        agent_id=target_agent_id,
        server_id=None,
        stack_id=None,
        assignment_status="pending",
        display_name=source.display_name,
        username=source.username,
        password_enc=source.password_enc,  # reuse ciphertext; never decrypted here
        broker=source.broker,
        version=1,
    )
    db.add(new_customer)

    try:
        # Flush to mint new_customer.id (server-default gen_random_uuid()).
        await db.flush()

        new_tis: list[TradeInstruction] = []
        for ti in source_tis:
            nti = TradeInstruction(
                customer_id=new_customer.id,
                isin=ti.isin,
                side=ti.side,
                section_name="",  # placeholder until the flush mints the id
                comment=ti.comment,
                version=1,
            )
            db.add(nti)
            new_tis.append(nti)

        if new_tis:
            await db.flush()  # mint each new TradeInstruction.id
            for nti in new_tis:
                nti.section_name = _build_section_name(
                    target_agent_id,
                    new_customer.id,
                    nti.id,
                    new_customer.broker,
                    nti.isin,
                    nti.side,
                )
            await db.flush()

        await _write_audit(
            db,
            actor_id=actor_id,
            action="customer.copy",
            target_id=new_customer.id,
            before=None,
            after={
                **_public_snapshot(new_customer),
                "source_customer_id": str(source.id),
                "copied_trade_instructions": len(new_tis),
            },
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(
            "target agent already has a customer for this broker / account"
        ) from exc

    await db.refresh(new_customer)
    return new_customer


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def update_customer(
    db: AsyncSession,
    customer_id: UUID,
    data: CustomerUpdate,
    actor_id: UUID,
) -> Customer:
    """Apply a partial, optimistic-locked update to a customer (account) row.

    The caller MUST echo the ``version`` they read from the row. If it
    doesn't match the current DB value :class:`OptimisticLockError` is
    raised — the router translates that to HTTP 409 with a "reload and
    retry" message.
    """
    customer = await get_customer(db, customer_id)
    if customer is None:
        raise LookupError(f"customer {customer_id} not found")

    if customer.version != data.version:
        raise OptimisticLockError(
            f"version mismatch: row has {customer.version}, "
            f"caller had {data.version}"
        )

    before = _public_snapshot(customer)

    changes = data.model_dump(exclude={"version"}, exclude_unset=True)
    # If the broker is being (re)set, validate it against the DB BEFORE the
    # mutation/flush (read-only pre-check; raises plain ValueError → friendly
    # 422 via the router, no 500, no rollback). Write the CANONICAL (lowercase)
    # code the registry returns, not the caller's casing.
    if "broker" in changes and changes["broker"] is not None:
        changes["broker"] = await _validate_broker(db, changes["broker"])
    for field, value in changes.items():
        if field == "password":
            customer.password_enc = fernet_encrypt(value)
        else:
            setattr(customer, field, value)

    customer.version += 1
    customer.updated_at = _now_utc()

    try:
        await db.flush()
        await _write_audit(
            db,
            actor_id=actor_id,
            action="customer.update",
            target_id=customer.id,
            before=before,
            after=_public_snapshot(customer),
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(
            "customer already exists for this agent / broker / account"
        ) from exc

    await db.refresh(customer)
    return customer


# ---------------------------------------------------------------------------
# Password decrypt (for the render layer)
# ---------------------------------------------------------------------------


async def decrypt_password(customer: Customer) -> str:
    """Decrypt a customer's stored password.

    The render layer calls it once per Customer per ``config.ini`` render —
    the result is reused across all that customer's TradeInstructions.
    :func:`app.security.crypto.decrypt` already emits a structured
    ``secret_decrypt`` audit log entry on every call.

    Async signature even though the underlying call is sync: the render
    layer is async top-to-bottom and we don't want callers to have to
    remember which helpers are awaitable and which aren't.
    """
    return fernet_decrypt(customer.password_enc)


__all__ = [
    "OptimisticLockError",
    "copy_customer_to_agent",
    "create_customer",
    "decrypt_password",
    "get_customer",
    "get_customer_trade_counts",
    "list_customers",
    "update_customer",
]
