"""Unit tests for the customer fee-payment ledger service (#116)."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import fee_payments as fp


# ---------------------------------------------------------------------------
# parse_amount
# ---------------------------------------------------------------------------

def test_parse_amount_strips_commas_and_spaces():
    assert fp.parse_amount("1,000,000") == Decimal("1000000")
    assert fp.parse_amount(" 250000 ") == Decimal("250000")


def test_parse_amount_rejects_nonpositive_and_garbage():
    with pytest.raises(ValueError):
        fp.parse_amount("0")
    with pytest.raises(ValueError):
        fp.parse_amount("-5")
    with pytest.raises(ValueError):
        fp.parse_amount("abc")


# ---------------------------------------------------------------------------
# record_payment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_payment_inserts_and_audits():
    added = []
    db = MagicMock()
    db.add = MagicMock(side_effect=lambda o: added.append(o))
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    cust = uuid.uuid4()
    actor = uuid.uuid4()
    pmt = await fp.record_payment(
        db, customer_id=cust, amount=Decimal("1000000"),
        paid_at=date(2026, 6, 1), note="Azadi", recorded_by=actor,
    )
    assert pmt.amount == Decimal("1000000") and pmt.customer_id == cust
    # one payment row + one audit row added; committed once.
    assert len(added) == 2
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# total_paid_by_customer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_total_paid_empty_ids_is_empty():
    db = MagicMock()
    db.execute = AsyncMock()
    out = await fp.total_paid_by_customer(db, [])
    assert out == {}
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_total_paid_sums_per_customer():
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    res = MagicMock()
    res.all.return_value = [(c1, Decimal("3000")), (c2, Decimal("500"))]
    db = MagicMock()
    db.execute = AsyncMock(return_value=res)
    out = await fp.total_paid_by_customer(db, [c1, c2])
    assert out == {c1: Decimal("3000"), c2: Decimal("500")}
