"""Manual per-customer close prices for the fee report.

Replaces the automatic 20-day mark-to-market: an open (unmatched) bot-buy
remainder of a (customer, ISIN) position is realized into the fee only when that
position has a saved close price here. The price is per customer — closing one
customer's holding of a symbol does NOT touch another customer's. It defaults to
the latest market price but is editable anytime (a stock can drop after a general
assembly / مجمع dividend) and the fee recomputes live; clearing the row re-opens
the position. Mirrors ``decline_order`` — every mutation writes an ``audit_log``
row (before/after) and is idempotent.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import desc, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.instrument_close_price import InstrumentClosePrice

_Key = tuple[UUID, str]


def _audit(actor_id, customer_id, isin, *, prev_price=None, prev_note=None,
           new_price=None, new_note=None, action: str) -> AuditLog:
    return AuditLog(
        actor_user_id=actor_id,
        action=action,
        target_type="instrument_close_price",
        target_id=str(isin),  # the ISIN, so the history can resolve the symbol
        before_json={
            "customer_id": str(customer_id),
            "close_price": str(prev_price) if prev_price is not None else None,
            "note": prev_note,
        },
        after_json={
            "customer_id": str(customer_id),
            "close_price": str(new_price) if new_price is not None else None,
            "note": new_note,
        },
    )


async def get_close_price(
    db: AsyncSession, customer_id: UUID, isin: str
) -> Optional[Decimal]:
    """The saved close price for one (customer, isin) position, or ``None``."""
    row = await db.get(InstrumentClosePrice, (customer_id, isin))
    return row.close_price if row is not None else None


async def get_close_prices(
    db: AsyncSession, pairs: Iterable[_Key]
) -> dict[_Key, Decimal]:
    """Batch-load close prices for (customer_id, isin) pairs.

    Used once at the top of the fee report so it never makes a per-position call.
    Empty input → ``{}`` (no query).
    """
    wanted = {(c, i) for c, i in pairs if c is not None and i}
    if not wanted:
        return {}
    rows = await db.execute(
        select(
            InstrumentClosePrice.customer_id,
            InstrumentClosePrice.isin,
            InstrumentClosePrice.close_price,
        ).where(
            tuple_(InstrumentClosePrice.customer_id, InstrumentClosePrice.isin).in_(
                list(wanted)
            )
        )
    )
    return {(c, i): p for c, i, p in rows.all()}


async def list_close_prices(db: AsyncSession) -> list[InstrumentClosePrice]:
    """All saved close prices, most-recently-updated first."""
    rows = await db.execute(
        select(InstrumentClosePrice).order_by(desc(InstrumentClosePrice.updated_at))
    )
    return list(rows.scalars().all())


async def set_close_price(
    db: AsyncSession,
    customer_id: UUID,
    isin: str,
    price: Decimal,
    note: Optional[str],
    actor_id: UUID,
    *,
    commit: bool = True,
) -> InstrumentClosePrice:
    """Upsert the close price for one (customer, isin) → realizes that position.

    Writes an ``instrument_close_price.set`` audit row. ``commit=False`` stages
    for a bulk caller. Caller validates ``price > 0``.
    """
    row = await db.get(InstrumentClosePrice, (customer_id, isin))
    prev_price = row.close_price if row is not None else None
    prev_note = row.note if row is not None else None
    db.add(_audit(
        actor_id, customer_id, isin, prev_price=prev_price, prev_note=prev_note,
        new_price=price, new_note=note, action="instrument_close_price.set",
    ))
    now = datetime.now(timezone.utc)
    if row is None:
        row = InstrumentClosePrice(
            customer_id=customer_id, isin=isin, close_price=price, note=note,
            updated_by=actor_id, updated_at=now,
        )
        db.add(row)
    else:
        row.close_price = price
        row.note = note
        row.updated_by = actor_id
        row.updated_at = now
    if commit:
        await db.commit()
    return row


async def set_close_price_if_absent(
    db: AsyncSession,
    customer_id: UUID,
    isin: str,
    price: Decimal,
    note: Optional[str],
    actor_id: UUID,
    *,
    commit: bool = True,
) -> bool:
    """Insert a close price ONLY if the (customer, isin) has none — race-safe.

    Used by the bulk "Close all" so a manual price set concurrently is never
    clobbered. Returns ``True`` if inserted (and audits), ``False`` if a price
    already existed. ``ON CONFLICT (customer_id, isin) DO NOTHING``.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(InstrumentClosePrice)
        .values(
            customer_id=customer_id, isin=isin, close_price=price, note=note,
            updated_by=actor_id, updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["customer_id", "isin"])
        .returning(InstrumentClosePrice.isin)
    )
    res = await db.execute(stmt)
    inserted = res.scalar_one_or_none() is not None
    if inserted:
        db.add(_audit(
            actor_id, customer_id, isin, new_price=price, new_note=note,
            action="instrument_close_price.set",
        ))
    if commit:
        await db.commit()
    return inserted


async def clear_close_price(
    db: AsyncSession,
    customer_id: UUID,
    isin: str,
    actor_id: UUID,
    *,
    commit: bool = True,
) -> bool:
    """Delete the close price for one (customer, isin) → re-opens that position.

    Returns ``True`` if a row was removed, ``False`` if none existed (idempotent,
    no audit/commit on a no-op).
    """
    row = await db.get(InstrumentClosePrice, (customer_id, isin))
    if row is None:
        return False
    db.add(_audit(
        actor_id, customer_id, isin, prev_price=row.close_price, prev_note=row.note,
        action="instrument_close_price.clear",
    ))
    await db.delete(row)
    if commit:
        await db.commit()
    return True


__all__ = [
    "get_close_price",
    "get_close_prices",
    "list_close_prices",
    "set_close_price",
    "set_close_price_if_absent",
    "clear_close_price",
]
