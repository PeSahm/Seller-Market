"""Customer fee-payment ledger (#116) — fees the operator has RECEIVED.

Admin records "customer X paid Y" rows; the fee report subtracts the per-customer
total from the computed owed to show what remains. Read side feeds both the
bot-report fees tab and the per-customer detail page.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.fees import CustomerFeePayment

logger = logging.getLogger(__name__)


def parse_amount(raw: str) -> Decimal:
    """Parse a money amount from a form field (commas/spaces tolerated).

    Raises ``ValueError`` (→ friendly 400) on non-numeric or non-positive input.
    """
    s = (raw or "").replace(",", "").replace(" ", "").strip()
    try:
        amount = Decimal(s)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid amount: {raw!r}") from exc
    if amount <= 0:
        raise ValueError("amount must be greater than zero")
    return amount


async def record_payment(
    db: AsyncSession,
    *,
    customer_id: UUID,
    amount: Decimal,
    paid_at: Optional[date],
    note: Optional[str],
    recorded_by: Optional[UUID],
) -> CustomerFeePayment:
    """Insert one received-fee payment row + an audit entry."""
    payment = CustomerFeePayment(
        customer_id=customer_id,
        amount=amount,
        paid_at=paid_at,
        note=(note or None),
        recorded_by=recorded_by,
    )
    db.add(payment)
    await db.flush()
    db.add(
        AuditLog(
            actor_user_id=recorded_by,
            action="customer.fee_payment",
            target_type="customer",
            target_id=str(customer_id),
            before_json=None,
            after_json={
                "payment_id": str(payment.id),
                "amount": str(amount),
                "paid_at": paid_at.isoformat() if paid_at else None,
                "note": note or None,
            },
        )
    )
    await db.commit()
    await db.refresh(payment)
    return payment


async def total_paid_by_customer(
    db: AsyncSession, customer_ids: list[UUID]
) -> dict[UUID, Decimal]:
    """``{customer_id: Σ amount}`` for the given customers (absent = 0)."""
    if not customer_ids:
        return {}
    rows = await db.execute(
        select(
            CustomerFeePayment.customer_id,
            func.coalesce(func.sum(CustomerFeePayment.amount), 0),
        )
        .where(CustomerFeePayment.customer_id.in_(customer_ids))
        .group_by(CustomerFeePayment.customer_id)
    )
    return {cid: Decimal(total) for cid, total in rows.all()}


async def list_payments(
    db: AsyncSession, customer_id: UUID
) -> list[CustomerFeePayment]:
    """All payments for one customer, newest-first."""
    rows = await db.execute(
        select(CustomerFeePayment)
        .where(CustomerFeePayment.customer_id == customer_id)
        .order_by(CustomerFeePayment.created_at.desc())
    )
    return list(rows.scalars().all())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "parse_amount",
    "record_payment",
    "total_paid_by_customer",
    "list_payments",
]
