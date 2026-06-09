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
* ``hard_delete_trade_instruction(db, id, actor_id)``

Each write emits a structured audit-log entry under target_type
``"trade_instruction"``. The delete audit row carries the pre-delete
snapshot in ``before_json`` and ``after_json=None`` — the row itself is
gone after the commit; the audit entry is the only forensic trace.

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

from sqlalchemy import delete, select
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
        "auto_sell_threshold": ti.auto_sell_threshold,
        "section_name": ti.section_name,
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
) -> list[TradeInstruction]:
    """Return all trade instructions for one customer."""
    stmt = (
        select(TradeInstruction)
        .where(TradeInstruction.customer_id == customer_id)
        .order_by(
            TradeInstruction.isin,
            TradeInstruction.side,
            TradeInstruction.created_at,
        )
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


async def list_armed_auto_sell(
    db: AsyncSession,
    agent_id: Optional[UUID] = None,
) -> list[tuple[TradeInstruction, Customer]]:
    """Return ``(TradeInstruction, Customer)`` pairs armed for auto-sell (#110).

    "Armed" = ``auto_sell_threshold`` is set and ``> 0``. Scoped to ``agent_id``
    when given (the agent's Active-auto-sell page), else all (admin). Joined to
    Customer so the page can show the owner + broker + account without a second
    round-trip.
    """
    stmt = (
        select(TradeInstruction, Customer)
        .join(Customer, Customer.id == TradeInstruction.customer_id)
        .where(TradeInstruction.auto_sell_threshold.isnot(None))
        .where(TradeInstruction.auto_sell_threshold > 0)
        .order_by(Customer.agent_id, TradeInstruction.isin)
    )
    if agent_id is not None:
        stmt = stmt.where(Customer.agent_id == agent_id)
    return list((await db.execute(stmt)).all())


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
        # Auto-sell only makes sense for a BUY. Normalize 0 → None so "disabled"
        # has ONE representation in the DB (the bot also treats <= 0 as disabled).
        auto_sell_threshold=(data.auto_sell_threshold if (data.side == 1 and data.auto_sell_threshold) else None),
        # Placeholder until the flush gives us the id.
        section_name="",
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

    # Auto-sell is BUY-only and "disabled" is a single representation: clear the
    # threshold on a SELL row (incl. a side 1->2 flip), and normalize 0 -> None.
    if ti.side != 1 or not ti.auto_sell_threshold:
        ti.auto_sell_threshold = None

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
# Delete (hard)
# ---------------------------------------------------------------------------


async def hard_delete_trade_instruction(
    db: AsyncSession,
    trade_instruction_id: UUID,
    actor_id: UUID,
) -> None:
    """Hard-delete a TradeInstruction — the row is gone after this commits.

    The audit row is written first (capturing the pre-delete snapshot in
    ``before_json``) and the DELETE runs in the same transaction, so a
    crash between the two either rolls both back or commits both. The
    audit ``target_id`` is free-form TEXT (not an FK) so it survives the
    deletion of the row it references.

    Idempotent: missing trade_instruction is a no-op.
    """
    ti = await get_trade_instruction(db, trade_instruction_id)
    if ti is None:
        return

    before = _public_snapshot(ti)
    await _write_audit(
        db,
        actor_id=actor_id,
        action="trade_instruction.delete",
        target_id=ti.id,
        before=before,
        after=None,
    )
    await db.delete(ti)
    await db.commit()


async def delete_all_for_agent(
    db: AsyncSession,
    agent_id: UUID,
    actor_id: UUID,
) -> tuple[int, list[UUID]]:
    """Hard-delete EVERY trade instruction across all of ``agent_id``'s customers.

    Returns ``(deleted_count, affected_stack_ids)`` — the caller re-pushes
    ``config.ini`` to each affected stack so the trading hosts drop the
    sections. One SUMMARY audit row is written (per-row snapshots would flood
    the log for a bulk clear); it records the count + a compact isin/side list.

    No-op (returns ``(0, [])``) when the agent has no instructions.
    """
    rows = (
        await db.execute(
            select(
                TradeInstruction.id,
                TradeInstruction.isin,
                TradeInstruction.side,
                Customer.stack_id,
            )
            .join(Customer, Customer.id == TradeInstruction.customer_id)
            .where(Customer.agent_id == agent_id)
        )
    ).all()
    if not rows:
        return 0, []

    ti_ids = [r[0] for r in rows]
    affected_stacks = sorted(
        {r[3] for r in rows if r[3] is not None}, key=str
    )
    deleted_brief = [{"isin": r[1], "side": r[2]} for r in rows]

    # Summary audit. target_type "agent" (the scope), not the individual rows.
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action="trade_instruction.delete_all",
            target_type="agent",
            target_id=str(agent_id),
            before_json={"count": len(ti_ids), "deleted": deleted_brief},
            after_json=None,
        )
    )
    await db.execute(delete(TradeInstruction).where(TradeInstruction.id.in_(ti_ids)))
    await db.commit()
    return len(ti_ids), list(affected_stacks)


__all__ = [
    "OptimisticLockError",
    "create_trade_instruction",
    "delete_all_for_agent",
    "get_trade_instruction",
    "hard_delete_trade_instruction",
    "list_trade_instructions",
    "update_trade_instruction",
]
