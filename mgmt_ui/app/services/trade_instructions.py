"""TradeInstruction CRUD orchestration.

A :class:`TradeInstruction` is "trade ISIN X on side Y for Customer Z".
One Customer can have many. After the 0003 split this is where the
isin/side/comment/section_name fields live; Customer is just the
account.

The service mirrors the shape of :mod:`app.services.customers`:

* ``list_trade_instructions(db, customer_id)`` — read
* ``get_trade_instruction(db, id)`` — single read
* ``create_trade_instruction(db, customer_id, data, actor_id)``
* ``update_trade_instruction(db, id, data, actor_id)`` (optimistic-locked)
* ``soft_delete_trade_instruction(db, id, actor_id)``

Each write emits a structured audit-log entry under target_type
``"trade_instruction"``.

Section name
------------
Globally unique header string for the ``[section]`` block that the
renderer writes into the on-server ``config.ini``. Format::

    a<8 hex of agent_id>_c<8 hex of customer_id>_t<8 hex of trade_inst_id>_
        <broker>_<isin>_s<side>

The trade_instruction id slice guarantees uniqueness even if the same
``(broker, isin, side)`` appears under two different customers — the
old Customer-only format would have collided across customers, but the
new format includes ``t<...>`` to disambiguate.
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
from app.models.trade_instructions import TradeInstruction
from app.schemas.trade_instruction import (
    TradeInstructionCreate,
    TradeInstructionUpdate,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OptimisticLockError(Exception):
    """Raised when an update's ``version`` doesn't match the row's current.

    Router catches → HTTP 409 with reload-and-retry message.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _build_section_name(
    agent_id: UUID,
    customer_id: UUID,
    trade_instruction_id: UUID,
    broker: str,
    isin: str,
    side: int,
) -> str:
    """Compose the globally-unique ``[section]`` header for a TradeInstruction.

    Format: ``a<8h>_c<8h>_t<8h>_<broker>_<isin>_s<side>``. The
    ``trade_instruction_id`` slice disambiguates against another
    instruction with the same (broker, isin, side) under a different
    customer. All hex chars come from ``UUID.hex`` so only ``[0-9a-f]``
    bytes reach the f-string — defense-in-depth against any future
    refactor that renders this string into a path.
    """
    a = agent_id.hex
    c = customer_id.hex
    t = trade_instruction_id.hex
    return f"a{a[:8]}_c{c[:8]}_t{t[:8]}_{broker}_{isin}_s{side}"


def _public_snapshot(ti: TradeInstruction) -> dict:
    """Audit-safe dict of a TradeInstruction row. No secrets to omit."""
    return {
        "id": str(ti.id),
        "customer_id": str(ti.customer_id),
        "isin": ti.isin,
        "side": ti.side,
        "section_name": ti.section_name,
        "enabled": ti.enabled,
        "comment": ti.comment,
        "version": ti.version,
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
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action=action,
            target_type="trade_instruction",
            target_id=str(target_id),
            before_json=before,
            after_json=after,
        )
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_trade_instructions(
    db: AsyncSession,
    customer_id: UUID,
    *,
    include_disabled: bool = True,
) -> list[TradeInstruction]:
    """Return all trade instructions for one customer.

    ``include_disabled`` defaults to True for admin UI use — the operator
    wants to see disabled ones so they can re-enable. The renderer
    explicitly passes False (via stacks.py's join clause) to hide them.
    """
    stmt = select(TradeInstruction).where(TradeInstruction.customer_id == customer_id)
    if not include_disabled:
        stmt = stmt.where(TradeInstruction.enabled.is_(True))
    stmt = stmt.order_by(
        TradeInstruction.isin, TradeInstruction.side, TradeInstruction.created_at
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_trade_instruction(
    db: AsyncSession, trade_instruction_id: UUID
) -> Optional[TradeInstruction]:
    """Look up a single trade instruction by id."""
    stmt = select(TradeInstruction).where(TradeInstruction.id == trade_instruction_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def create_trade_instruction(
    db: AsyncSession,
    customer_id: UUID,
    data: TradeInstructionCreate,
    actor_id: UUID,
) -> TradeInstruction:
    """Insert a new trade instruction under ``customer_id``.

    Raises ``LookupError`` if the customer doesn't exist; ``ValueError``
    if the (customer, isin, side) tuple is already in use on this
    customer.
    """
    # Need the parent customer for agent_id + broker (used in section_name).
    customer_stmt = select(Customer).where(Customer.id == customer_id)
    customer = (await db.execute(customer_stmt)).scalar_one_or_none()
    if customer is None:
        raise LookupError(f"customer {customer_id} not found")

    ti = TradeInstruction(
        customer_id=customer_id,
        isin=data.isin,
        side=data.side,
        # Placeholder until the flush gives us the id.
        section_name="",
        enabled=True,
        comment=data.comment,
        version=1,
    )
    db.add(ti)

    try:
        await db.flush()
        ti.section_name = _build_section_name(
            customer.agent_id, customer.id, ti.id, customer.broker, ti.isin, ti.side
        )
        await _write_audit(
            db,
            actor_id=actor_id,
            action="trade_instruction.create",
            target_id=ti.id,
            before=None,
            after=_public_snapshot(ti),
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(
            "trade already exists for this customer / symbol / side"
        ) from exc

    await db.refresh(ti)
    return ti


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def update_trade_instruction(
    db: AsyncSession,
    trade_instruction_id: UUID,
    data: TradeInstructionUpdate,
    actor_id: UUID,
) -> TradeInstruction:
    """Partial update with optimistic locking.

    If ``isin`` or ``side`` change, the section_name is regenerated to
    stay grep-friendly in the on-server config.ini.
    """
    ti = await get_trade_instruction(db, trade_instruction_id)
    if ti is None:
        raise LookupError(f"trade_instruction {trade_instruction_id} not found")

    if ti.version != data.version:
        raise OptimisticLockError(
            f"version mismatch: row has {ti.version}, caller had {data.version}"
        )

    before = _public_snapshot(ti)

    changes = data.model_dump(exclude={"version"}, exclude_unset=True)
    for field, value in changes.items():
        setattr(ti, field, value)

    # Section name embeds isin+side — regenerate when either changed.
    if "isin" in data.model_fields_set or "side" in data.model_fields_set:
        customer_stmt = select(Customer).where(Customer.id == ti.customer_id)
        customer = (await db.execute(customer_stmt)).scalar_one()
        ti.section_name = _build_section_name(
            customer.agent_id, customer.id, ti.id, customer.broker, ti.isin, ti.side
        )

    ti.version += 1
    ti.updated_at = _now_utc()

    try:
        await db.flush()
        await _write_audit(
            db,
            actor_id=actor_id,
            action="trade_instruction.update",
            target_id=ti.id,
            before=before,
            after=_public_snapshot(ti),
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(
            "trade already exists for this customer / symbol / side"
        ) from exc

    await db.refresh(ti)
    return ti


# ---------------------------------------------------------------------------
# Delete (soft)
# ---------------------------------------------------------------------------


async def soft_delete_trade_instruction(
    db: AsyncSession,
    trade_instruction_id: UUID,
    actor_id: UUID,
) -> None:
    """Soft-delete a TradeInstruction by flipping ``enabled=False``.

    Same idempotency rule as soft_delete_customer: no-op for missing /
    already-disabled rows, no duplicate audit entries.
    """
    ti = await get_trade_instruction(db, trade_instruction_id)
    if ti is None:
        return
    if not ti.enabled:
        return

    before = _public_snapshot(ti)
    ti.enabled = False
    ti.version += 1
    ti.updated_at = _now_utc()

    await _write_audit(
        db,
        actor_id=actor_id,
        action="trade_instruction.delete",
        target_id=ti.id,
        before=before,
        after=_public_snapshot(ti),
    )
    await db.commit()


__all__ = [
    "OptimisticLockError",
    "create_trade_instruction",
    "get_trade_instruction",
    "list_trade_instructions",
    "soft_delete_trade_instruction",
    "update_trade_instruction",
]
