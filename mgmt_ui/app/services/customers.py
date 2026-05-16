"""Customer CRUD orchestration (Phase 4).

A ``Customer`` row models one agent-owned trading account: broker credentials
plus an (ISIN, side) pair. This module is the seam between the HTTP routers
(admin and agent) and the lower-level pieces:

* :mod:`app.models.customers` for the DB row
* :mod:`app.security.crypto` for Fernet password encryption/decryption
* :mod:`app.models.audit` for write-side audit logging

The router stays thin: it converts a form into a pydantic model, calls one
function here, and renders the result. Anything involving more than a single
SQL statement lives here.

Section name format
-------------------
Every customer has a globally-unique ``section_name`` that the render layer
uses verbatim as the ``[section]`` header in a remote ``config.ini``. The
format is::

    a<8 hex>_c<8 hex>_<broker>_<isin>

— for example ``a4eebf408_c04cdabd0_bbi_IRO3AYHZ0001``. We slice the
agent and customer UUIDs to their first 8 hex chars (out of 32) to keep
the section name short and human-readable. Collisions across the lifetime
of the system are negligible: with N customers, the birthday-paradox
collision probability is roughly N^2 / 2^33, so we'd need on the order of
~90k customers before P(collision) hits 0.5. The DB ``UNIQUE`` constraint
on ``section_name`` guarantees actual uniqueness — if a collision ever
happened the insert would fail and the caller would see a clear
``ValueError``.

Why ``.hex`` slicing and not ``str(uuid)``?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The section name doesn't reach a filesystem call here (it's just an INI
section header), so the Phase 2 ``py/path-injection`` CodeQL concern
doesn't strictly apply. But we still route through ``.hex`` to stay
consistent with the "only hex chars cross the boundary" idiom — that
makes future refactors that DO render this string into a path safe
by default.

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

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.customers import Customer
from app.schemas.customer import CustomerCreate, CustomerDuplicate, CustomerUpdate
from app.security.crypto import decrypt as fernet_decrypt
from app.security.crypto import encrypt as fernet_encrypt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OptimisticLockError(Exception):
    """Raised when an update's ``version`` doesn't match the row's current.

    The router catches this and returns HTTP 409 with a message asking the
    user to reload and retry. We keep a typed exception (instead of a generic
    ``ValueError``) because the router needs to distinguish it from the
    UNIQUE-constraint ``ValueError`` raised on duplicate (agent, account,
    broker, isin, side) tuples.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    """A timezone-aware UTC ``datetime`` for TIMESTAMPTZ columns."""
    return datetime.now(timezone.utc)


def _build_section_name(
    agent_id: UUID,
    customer_id: UUID,
    broker: str,
    isin: str,
) -> str:
    """Compose the canonical ``[section]`` header for a customer.

    Format: ``a<8 hex>_c<8 hex>_<broker>_<isin>``.

    We slice the UUIDs to their first 8 hex chars so the section name stays
    under ~40 characters total — long enough to be unique in practice (see
    the module-level docstring for the birthday-paradox math) but short
    enough to be readable in a hand-edited ``config.ini``. We always use
    ``.hex`` slicing rather than ``str(uuid)`` so only ``[0-9a-f]`` chars
    ever reach the f-string — same defense-in-depth idiom as
    :func:`app.services.servers._key_path_for`.
    """
    a = agent_id.hex  # 32 lowercase hex chars, no separators
    c = customer_id.hex
    return f"a{a[:8]}_c{c[:8]}_{broker}_{isin}"


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
        "section_name": customer.section_name,
        "username": customer.username,
        "broker": customer.broker,
        "isin": customer.isin,
        "side": customer.side,
        "enabled": customer.enabled,
        "comment": customer.comment,
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
    include_disabled: bool = False,
) -> list[Customer]:
    """List customers, optionally filtered.

    ``include_disabled=False`` (the default) hides rows that have been
    soft-deleted (``enabled=False``). The render layer and the agent
    dashboard both pass the default; only the admin "all customers" view
    sets ``include_disabled=True`` to surface them for audit.

    The result is ordered by ``display_name`` for a stable UX. We don't
    paginate here — the expected fleet size (low thousands of customers)
    fits comfortably in one response; if that changes we'll add a cursor
    parameter rather than retrofitting offset-based pagination.
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
    if not include_disabled:
        stmt = stmt.where(Customer.enabled.is_(True))
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


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def create_customer(
    db: AsyncSession,
    agent_id: UUID,
    data: CustomerCreate,
    actor_id: UUID,
) -> Customer:
    """Insert a new customer owned by ``agent_id``.

    Order of operations (matches :func:`app.services.servers.create_server`):

    1. Insert the row with placeholder ``section_name=""`` and
       ``password_enc=b""`` so we can flush and let the DB-side default
       ``gen_random_uuid()`` populate ``customer.id``.
    2. Compute the section name from the new id.
    3. Fernet-encrypt the password.
    4. Patch the row, write the audit log, commit.

    Raises ``ValueError`` if the composite UNIQUE constraint
    ``(agent_id, username, broker, isin, side)`` fires — i.e. this agent
    already has a customer with the same credentials and instrument. The
    router translates that to a friendly 422.
    """
    customer = Customer(
        agent_id=agent_id,
        server_id=None,
        stack_id=None,
        assignment_status="pending",
        display_name=data.display_name,
        # Placeholders, patched below after the flush gives us the id.
        section_name="",
        username=data.username,
        password_enc=b"",
        broker=data.broker,
        isin=data.isin,
        side=data.side,
        enabled=True,
        comment=data.comment,
        version=1,
    )
    db.add(customer)

    try:
        # Flush so the DB-side default ``gen_random_uuid`` populates
        # ``customer.id`` before we use it in ``_build_section_name``.
        await db.flush()

        customer.section_name = _build_section_name(
            agent_id, customer.id, data.broker, data.isin
        )
        customer.password_enc = fernet_encrypt(data.password)

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
        # The composite UNIQUE on (agent_id, username, broker, isin, side)
        # fired. Re-raise as a clean ValueError per the Phase 3 review
        # convention so the router can show a friendly error.
        raise ValueError(
            "customer already exists for this agent / account / broker / "
            "symbol / side"
        ) from exc

    await db.refresh(customer)
    return customer


async def duplicate_customer(
    db: AsyncSession,
    source_id: UUID,
    data: CustomerDuplicate,
    actor_id: UUID,
) -> Customer:
    """Clone a customer to a new ISIN.

    The new row inherits the source's agent, broker, username, side, and
    encrypted password — the agent doesn't have to re-type credentials they
    already entered for a different instrument on the same brokerage account.
    Everything else (server / stack assignment, status, enabled flag, version)
    resets to "fresh row" defaults so the duplicate goes through the normal
    distribution path on next render.

    Raises ``LookupError`` if the source doesn't exist, and ``ValueError`` if
    the resulting tuple (agent, username, broker, new-isin, side) would
    duplicate an existing row.
    """
    source = await get_customer(db, source_id)
    if source is None:
        raise LookupError(f"customer {source_id} not found")

    display_name = (
        data.new_display_name
        if data.new_display_name is not None
        else f"{source.display_name} ({data.isin})"
    )

    clone = Customer(
        agent_id=source.agent_id,
        server_id=None,
        stack_id=None,
        assignment_status="pending",
        display_name=display_name,
        section_name="",  # patched after flush
        username=source.username,
        password_enc=b"",  # patched after flush (re-using source ciphertext)
        broker=source.broker,
        isin=data.isin,
        side=source.side,
        enabled=True,
        comment=source.comment,
        version=1,
    )
    db.add(clone)

    try:
        await db.flush()
        clone.section_name = _build_section_name(
            source.agent_id, clone.id, source.broker, data.isin
        )
        # Re-use the source's ciphertext directly — Fernet ciphertext is
        # nondeterministic, so re-encrypting the same plaintext would
        # produce a different token. Copying bytes keeps the audit-log
        # "secret_decrypt" counter accurate (we don't decrypt to clone).
        clone.password_enc = bytes(source.password_enc)

        await _write_audit(
            db,
            actor_id=actor_id,
            action="customer.create",
            target_id=clone.id,
            before=None,
            after={
                **_public_snapshot(clone),
                "duplicated_from": str(source.id),
            },
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(
            "customer already exists for this agent / account / broker / "
            "symbol / side"
        ) from exc

    await db.refresh(clone)
    return clone


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def update_customer(
    db: AsyncSession,
    customer_id: UUID,
    data: CustomerUpdate,
    actor_id: UUID,
) -> Customer:
    """Apply a partial, optimistic-locked update to a customer row.

    The caller MUST echo the ``version`` they read from the row. If it doesn't
    match the current DB value :class:`OptimisticLockError` is raised — the
    router translates that to HTTP 409 with a "reload and retry" message.

    If ``broker`` or ``isin`` change, the section name is regenerated to keep
    it in sync with the row's identity (the render layer relies on
    ``section_name`` containing the current broker+isin for grep-ability
    during incident response).

    A successful update bumps ``version`` and stamps ``updated_at``. The
    plaintext password is only touched if ``data.password`` was explicitly
    set; ``None`` means "keep existing".
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

    # Apply only fields the caller explicitly set. ``exclude_unset=True`` is
    # critical here: ``exclude_none=True`` would also drop ``enabled=False``
    # which the caller may very well want to set explicitly.
    changes = data.model_dump(exclude={"version"}, exclude_unset=True)
    for field, value in changes.items():
        if field == "password":
            # Fernet-encrypt the new plaintext. The plaintext is dropped at
            # the end of this function — never logged, never persisted in any
            # form other than the ciphertext.
            customer.password_enc = fernet_encrypt(value)
        else:
            setattr(customer, field, value)

    # Section name embeds broker+isin, so regenerate when either changed.
    if "broker" in data.model_fields_set or "isin" in data.model_fields_set:
        customer.section_name = _build_section_name(
            customer.agent_id, customer.id, customer.broker, customer.isin
        )

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
        # Same composite UNIQUE as create_customer — a partial update can
        # absolutely cause the (agent, account, broker, isin, side) tuple to
        # collide with another row.
        raise ValueError(
            "customer already exists for this agent / account / broker / "
            "symbol / side"
        ) from exc

    await db.refresh(customer)
    return customer


# ---------------------------------------------------------------------------
# Delete (soft)
# ---------------------------------------------------------------------------


async def soft_delete_customer(
    db: AsyncSession,
    customer_id: UUID,
    actor_id: UUID,
) -> None:
    """Soft-delete a customer.

    Marks ``enabled=False``, resets ``assignment_status='pending'`` and
    ``stack_id=NULL``. The render layer (Phase 4) filters disabled rows out
    of the next ``config.ini`` push, so the agent / bot effectively stops
    trading this customer on the next render cycle without us touching the
    remote server's filesystem from inside this function.

    Idempotent: deleting a missing or already-disabled customer is a no-op
    (we don't write a second audit row in that case — re-deleting isn't a
    meaningful event).

    We keep the row + audit history forever (no hard-delete). Phase 9 may
    add a retention-window cleanup once we know the legal/compliance
    requirements for trading-account records.
    """
    customer = await get_customer(db, customer_id)
    if customer is None:
        return
    if not customer.enabled:
        # Already soft-deleted; skip the no-op write to keep audit history
        # clean.
        return

    before = _public_snapshot(customer)
    customer.enabled = False
    customer.assignment_status = "pending"
    customer.stack_id = None
    customer.version += 1
    customer.updated_at = _now_utc()

    await _write_audit(
        db,
        actor_id=actor_id,
        action="customer.delete",
        target_id=customer.id,
        before=before,
        after=_public_snapshot(customer),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Password decrypt (for the render layer)
# ---------------------------------------------------------------------------


async def decrypt_password(customer: Customer) -> str:
    """Decrypt a customer's stored password.

    This is the one and only function in the codebase that should ever turn
    ``Customer.password_enc`` back into plaintext. The render layer calls it
    once per customer per ``config.ini`` render. :func:`app.security.crypto.decrypt`
    already emits a structured ``secret_decrypt`` audit log entry on every
    call, so an audit-log subscriber can count decryptions and alert on
    anomalies — that's the "secret hygiene" rule from the plan.

    Async signature even though the underlying call is sync: the render
    layer is async top-to-bottom and we don't want callers to have to
    remember which helpers are awaitable and which aren't.
    """
    return fernet_decrypt(customer.password_enc)


__all__ = [
    "OptimisticLockError",
    "create_customer",
    "decrypt_password",
    "duplicate_customer",
    "get_customer",
    "list_customers",
    "soft_delete_customer",
    "update_customer",
]
