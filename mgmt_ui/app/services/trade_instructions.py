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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

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
        "auto_sell_only": ti.auto_sell_only,
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
        # Watch-only flag — the Create validator guarantees a flagged row has
        # side == 1 AND a threshold > 0, so it survives the normalization above.
        auto_sell_only=data.auto_sell_only,
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
        raise ValueError(_duplicate_tuple_message(data.side)) from exc

    await db.refresh(ti)
    return ti


def _duplicate_tuple_message(side: int) -> str:
    """Friendly text for the UNIQUE (customer, isin, side) collision.

    side=1 names BOTH row kinds that occupy the slot (a Buy and an
    Auto-sell-only watch are stored identically as side=1); side=2 keeps the
    plain Sell wording — mentioning watch-only rows there would send the
    operator hunting for a row that can't exist.
    """
    if side == 2:
        return (
            "a Sell instruction already exists for this "
            "customer / symbol / side — edit it instead"
        )
    return (
        "a Buy or Auto-sell-only instruction already exists for this "
        "customer / symbol / side — edit it instead"
    )


# ---------------------------------------------------------------------------
# Bulk create (one instrument → many customers)
# ---------------------------------------------------------------------------


@dataclass
class BulkCreateResult:
    """Outcome of a bulk trade-instruction create across many customers.

    ``skipped`` are customers that already had a row for ``(isin, side)`` —
    they keep their existing row untouched (skip & report, not overwrite).
    ``missing`` are ids that weren't found, or (when ``expected_agent_id`` is
    set) belong to a different agent. ``affected_stack_ids`` is the de-duped,
    NULL-dropped set of stacks the caller must re-push ``config.ini`` to.
    """

    created_count: int = 0
    created_customer_ids: list[UUID] = field(default_factory=list)
    skipped_customer_ids: list[UUID] = field(default_factory=list)
    missing_customer_ids: list[UUID] = field(default_factory=list)
    affected_stack_ids: list[UUID] = field(default_factory=list)


async def bulk_create_trade_instruction(
    db: AsyncSession,
    customer_ids: list[UUID],
    data: TradeInstructionCreate,
    actor_id: UUID,
    *,
    expected_agent_id: Optional[UUID] = None,
) -> BulkCreateResult:
    """Create ONE trade instruction (``data``) across MANY customers.

    Per-customer duplicates are SKIPPED, not errors: a customer that already
    has a row for ``(data.isin, data.side)`` is recorded in
    ``skipped_customer_ids`` and the rest still get created. ``data`` is
    already mapped (``data.side`` is the STORED side 1/2 and
    ``data.auto_sell_only`` the watch-only flag — the form's side=3 alias was
    resolved by ``map_side_form`` in the route), so the duplicate check uses
    ``data.side`` verbatim.

    Writes ONE summary audit row and commits ONCE (mirrors
    :func:`delete_all_for_agent`). Returns a :class:`BulkCreateResult`; the
    caller re-pushes ``config.ini`` to each ``affected_stack_ids`` entry.

    ``expected_agent_id`` (set by the agent route to ``user.id``) drops any
    customer owned by a different agent into ``missing_customer_ids`` —
    defence in depth behind the route's ownership gate. Admin passes ``None``.

    Raises ``ValueError`` only on the rare TOCTOU collision (a concurrent
    writer inserts the same tuple between our pre-query and our flush).
    """
    result = BulkCreateResult()

    # De-dup the posted ids — a double-ticked box (or duplicated markup) must
    # not create two rows / two skips for one customer.
    requested = list(dict.fromkeys(customer_ids))
    if not requested:
        return result  # empty selection — no audit, no commit

    # One SELECT of the candidates → primitive tuples (id, agent_id, broker,
    # stack_id). We read EVERYTHING we need here so nothing touches an ORM row
    # after the commit expires it (PR #73 MissingGreenlet trap).
    cust_rows = (
        await db.execute(
            select(
                Customer.id,
                Customer.agent_id,
                Customer.broker,
                Customer.stack_id,
            ).where(Customer.id.in_(requested))
        )
    ).all()
    cust_by_id = {r[0]: r for r in cust_rows}

    eligible_ids: list[UUID] = []
    for cid in requested:
        row = cust_by_id.get(cid)
        if row is None or (
            expected_agent_id is not None and row[1] != expected_agent_id
        ):
            result.missing_customer_ids.append(cid)
            continue
        eligible_ids.append(cid)

    if not eligible_ids:
        return result  # nothing actionable — no audit, no commit

    # Pre-query existing rows for THIS (isin, side) across the eligible set so
    # duplicates are SKIPPED (not an aborting IntegrityError). Cheaper than a
    # per-row savepoint and matches the bulk contract (one SELECT, one commit).
    existing = set(
        (
            await db.execute(
                select(TradeInstruction.customer_id).where(
                    TradeInstruction.customer_id.in_(eligible_ids),
                    TradeInstruction.isin == data.isin,
                    TradeInstruction.side == data.side,
                )
            )
        )
        .scalars()
        .all()
    )

    to_create: list[UUID] = []
    for cid in eligible_ids:
        if cid in existing:
            result.skipped_customer_ids.append(cid)
        else:
            to_create.append(cid)

    if not to_create:
        # Every eligible customer already had the row — honest no-op (no audit,
        # no commit), mirroring delete_all_for_agent's empty contract.
        return result

    # Auto-sell only makes sense for a BUY; normalize 0 → None so "disabled"
    # has ONE representation (same as create_trade_instruction).
    threshold = (
        data.auto_sell_threshold
        if (data.side == 1 and data.auto_sell_threshold)
        else None
    )

    new_rows: list[tuple[UUID, TradeInstruction]] = []
    for cid in to_create:
        _, agent_id, broker, _ = cust_by_id[cid]
        # Mint the id in Python so the (globally-UNIQUE) section_name can be
        # computed up front and INSERTed directly. The single-row create gets
        # away with a "" placeholder + a follow-up UPDATE, but a BATCH of 2+
        # rows would all carry "" at the single flush → they'd collide on the
        # section_name UNIQUE. Building the final name before insert avoids it.
        tid = uuid4()
        ti = TradeInstruction(
            id=tid,
            customer_id=cid,
            isin=data.isin,
            side=data.side,
            auto_sell_threshold=threshold,
            auto_sell_only=data.auto_sell_only,
            section_name=_build_section_name(
                agent_id, cid, tid, broker, data.isin, data.side
            ),
            comment=data.comment,
            version=1,
        )
        db.add(ti)
        new_rows.append((cid, ti))

    # ONE summary audit row — per-row snapshots would flood the log.
    # _write_audit hardcodes target_type="trade_instruction" + a single trade
    # id, which is wrong for a batch, so inline the AuditLog (the same reasoning
    # delete_all_for_agent uses).
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action="trade_instruction.bulk_create",
            target_type="agent" if expected_agent_id else "user",
            target_id=str(expected_agent_id or actor_id),
            before_json=None,
            after_json={
                "isin": data.isin,
                "side": data.side,
                "auto_sell_only": data.auto_sell_only,
                "auto_sell_threshold": threshold,
                "created_count": len(new_rows),
                "created_customer_ids": [str(c) for c, _ in new_rows],
                "skipped_customer_ids": [
                    str(c) for c in result.skipped_customer_ids
                ],
            },
        )
    )

    try:
        await db.commit()
    except IntegrityError as exc:
        # TOCTOU: a concurrent writer inserted the same (customer, isin, side)
        # between our pre-query and this commit (or an astronomically-unlikely
        # section_name uuid clash).
        await db.rollback()
        raise ValueError(_duplicate_tuple_message(data.side)) from exc

    # Post-commit: assemble from the primitives captured above — NEVER read the
    # now-expired ORM rows.
    result.created_count = len(new_rows)
    result.created_customer_ids = [cid for cid, _ in new_rows]
    result.affected_stack_ids = sorted(
        {cust_by_id[cid][3] for cid, _ in new_rows if cust_by_id[cid][3] is not None},
        key=str,
    )
    return result


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

    # Validate the EFFECTIVE post-update state BEFORE mutating the ORM row —
    # raising after setattr would leave the session dirty, and the routers'
    # ValueError path calls db.refresh(user) whose autoflush would write the
    # half-applied state to the DB.
    eff_side = changes.get("side", ti.side)
    eff_threshold = changes.get("auto_sell_threshold", ti.auto_sell_threshold)
    if eff_side != 1 or not eff_threshold:
        # Auto-sell is BUY-only and "disabled" is a single representation:
        # clear the threshold on a SELL row (incl. a side 1->2 flip) and
        # normalize 0 -> None.
        eff_threshold = None
    # The flag is applied only when the payload explicitly carries it
    # (exclude_unset — the form posts side 1/2 with auto_sell_only=False, so
    # the flag never sticks across a side change); a flagged row that ends up
    # without side=1 + a threshold is meaningless.
    eff_auto_only = changes.get("auto_sell_only", ti.auto_sell_only)
    if eff_auto_only and (eff_side != 1 or not eff_threshold):
        raise ValueError(
            "auto-sell-only instruction needs a buy-queue threshold — "
            "delete the instruction instead of clearing it"
        )

    for attr, value in changes.items():
        setattr(ti, attr, value)
    ti.auto_sell_threshold = eff_threshold

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
        # eff_side is a plain int captured pre-mutation — never touch ORM
        # attrs after a rollback (PR #73 MissingGreenlet lesson).
        raise ValueError(_duplicate_tuple_message(eff_side)) from exc

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
    "BulkCreateResult",
    "OptimisticLockError",
    "bulk_create_trade_instruction",
    "create_trade_instruction",
    "delete_all_for_agent",
    "get_trade_instruction",
    "hard_delete_trade_instruction",
    "list_trade_instructions",
    "update_trade_instruction",
]
