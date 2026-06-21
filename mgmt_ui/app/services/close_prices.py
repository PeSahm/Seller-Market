"""Manual GLOBAL per-instrument close prices for the fee report.

Replaces the automatic 20-day mark-to-market: an open (unmatched) bot-buy
remainder is realized into the fee only when its ISIN has a saved close price
here. The price is editable anytime (a stock can drop after a general assembly /
مجمع dividend) and the fee recomputes live; clearing the row re-opens the
position. Mirrors the per-order ``decline_order`` shape — every mutation writes
an ``audit_log`` row (before/after) and is idempotent.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.instrument_close_price import InstrumentClosePrice


async def get_close_price(db: AsyncSession, isin: str) -> Optional[Decimal]:
    """The saved global close price for ``isin``, or ``None`` if unset."""
    row = await db.get(InstrumentClosePrice, isin)
    return row.close_price if row is not None else None


async def get_close_prices(
    db: AsyncSession, isins: Iterable[str]
) -> dict[str, Decimal]:
    """Batch-load saved close prices for ``isins`` → ``{isin: close_price}``.

    Used once at the top of the fee report so it never makes a per-ISIN call.
    Empty input → ``{}`` (no query).
    """
    wanted = {i for i in isins if i}
    if not wanted:
        return {}
    rows = await db.execute(
        select(InstrumentClosePrice.isin, InstrumentClosePrice.close_price).where(
            InstrumentClosePrice.isin.in_(wanted)
        )
    )
    return {isin: price for isin, price in rows.all()}


async def list_close_prices(db: AsyncSession) -> list[InstrumentClosePrice]:
    """All saved close prices, most-recently-updated first."""
    rows = await db.execute(
        select(InstrumentClosePrice).order_by(desc(InstrumentClosePrice.updated_at))
    )
    return list(rows.scalars().all())


async def set_close_price(
    db: AsyncSession,
    isin: str,
    price: Decimal,
    note: Optional[str],
    actor_id: UUID,
    *,
    commit: bool = True,
) -> InstrumentClosePrice:
    """Upsert the global close price for ``isin`` → realizes its open positions.

    Writes an ``instrument_close_price.set`` audit row (before/after). Pass
    ``commit=False`` from a bulk route to stage many sets under one commit (the
    caller commits). Caller validates ``price > 0``.
    """
    row = await db.get(InstrumentClosePrice, isin)
    prev_price = row.close_price if row is not None else None
    prev_note = row.note if row is not None else None
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action="instrument_close_price.set",
            target_type="instrument_close_price",
            target_id=str(isin),
            before_json={
                "close_price": str(prev_price) if prev_price is not None else None,
                "note": prev_note,
            },
            after_json={"close_price": str(price), "note": note},
        )
    )
    now = datetime.now(timezone.utc)
    if row is None:
        row = InstrumentClosePrice(
            isin=isin, close_price=price, note=note, updated_by=actor_id, updated_at=now
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


async def clear_close_price(
    db: AsyncSession,
    isin: str,
    actor_id: UUID,
    *,
    commit: bool = True,
) -> bool:
    """Delete the close price for ``isin`` → re-opens its positions (no fee).

    Returns ``True`` if a row was removed, ``False`` if none existed (idempotent,
    no audit, no commit on a no-op — matches ``undo_decline``).
    """
    row = await db.get(InstrumentClosePrice, isin)
    if row is None:
        return False
    prev_price = row.close_price
    prev_note = row.note
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action="instrument_close_price.clear",
            target_type="instrument_close_price",
            target_id=str(isin),
            before_json={
                "close_price": str(prev_price) if prev_price is not None else None,
                "note": prev_note,
            },
            after_json={"close_price": None},
        )
    )
    await db.delete(row)
    if commit:
        await db.commit()
    return True


__all__ = [
    "get_close_price",
    "get_close_prices",
    "list_close_prices",
    "set_close_price",
    "clear_close_price",
]
