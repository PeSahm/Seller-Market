"""Engine tests for the buy-side fee report + per-customer rollup (#116).

Fee = X% of the positive realized profit on bot buys (matched against ALL later
sells, manual or bot). The report also rolls up per customer (owed − paid =
remaining) and lists EVERY customer with orders in scope (show-all). The DB
query, the fee resolver, and the payments ledger are mocked.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.broker_orders import BrokerOrder
from app.services import profit_report as pr

_AGENT = uuid.uuid4()
_CUST = uuid.uuid4()
_CUST_B = uuid.uuid4()

# Real resolver captured before the autouse fixture stubs the module name.
_REAL_GET_FEE_PERCENT = pr.get_fee_percent


def _order(side, qty, price, *, is_bot, ts, tracking, cust=_CUST, isin="IRO1XXXX0001"):
    return BrokerOrder(
        customer_id=cust, agent_id=_AGENT, broker="ayandeh",
        account_username="u", tracking_number=tracking, isin=isin, symbol="X",
        order_side=side, price=Decimal(str(price)), volume=qty, executed_volume=qty,
        state=3, is_done=True, is_bot=is_bot, placed_at=ts, execution_date=ts,
        raw_json={},
    )


def _fake_db(orders, cust_agent_rows):
    class _OrdersRes:
        def scalars(self):
            return self

        def all(self):
            return orders

    class _RowsRes:
        def all(self):
            return cust_agent_rows

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_OrdersRes(), _RowsRes()])
    return db


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    async def _fee(_db, _agent, customer_id=None):
        return Decimal("1.0")  # 1%
    monkeypatch.setattr(pr, "get_fee_percent", _fee)

    async def _paid(_db, _ids):
        return {}
    monkeypatch.setattr(pr.fee_payments, "total_paid_by_customer", _paid)


async def test_realized_profit_fee_on_bot_buy():
    # bot buy 100@6000, then a (manual) sell 100@6500 → realized 50000, fee 1% = 500.
    buy = _order(1, 100, 6000, is_bot=True,
                 ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1)
    sell = _order(2, 100, 6500, is_bot=False,  # manual sell still realizes the profit
                  ts=datetime(2026, 6, 10, tzinfo=timezone.utc), tracking=2)
    rep = await pr.build_fee_report(_fake_db([buy, sell], [(_CUST, _AGENT)]))
    assert len(rep.buy_rows) == 1
    assert rep.buy_rows[0].realized_profit == Decimal("50000")
    assert rep.buy_rows[0].fee == Decimal("500")
    assert rep.grand_fee == Decimal("500")
    ct = rep.per_customer[_CUST]
    assert ct.total_fee == Decimal("500") and ct.realized_profit == Decimal("50000")


async def test_show_all_lists_zero_fee_customer():
    # Customer B has an order in scope but NO bot buy → still appears at zero,
    # so it's reachable for fee config + payment.
    a_buy = _order(1, 100, 6000, is_bot=True,
                   ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1)
    b_sell = _order(2, 50, 7000, is_bot=False,
                    ts=datetime(2026, 6, 2, tzinfo=timezone.utc), tracking=2, cust=_CUST_B)
    rep = await pr.build_fee_report(
        _fake_db([a_buy, b_sell], [(_CUST, _AGENT), (_CUST_B, _AGENT)])
    )
    assert _CUST in rep.per_customer and _CUST_B in rep.per_customer
    assert rep.per_customer[_CUST_B].total_fee == Decimal("0")
    assert rep.per_customer[_CUST_B].num_buys == 0


async def test_paid_and_remaining(monkeypatch):
    async def _paid(_db, _ids):
        return {_CUST: Decimal("200")}
    monkeypatch.setattr(pr.fee_payments, "total_paid_by_customer", _paid)
    buy = _order(1, 100, 6000, is_bot=True,
                 ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1)
    sell = _order(2, 100, 6500, is_bot=False,
                  ts=datetime(2026, 6, 10, tzinfo=timezone.utc), tracking=2)
    rep = await pr.build_fee_report(_fake_db([buy, sell], [(_CUST, _AGENT)]))
    ct = rep.per_customer[_CUST]
    assert ct.total_fee == Decimal("500")
    assert ct.paid == Decimal("200") and ct.remaining == Decimal("300")


async def test_loss_lot_earns_no_fee():
    buy = _order(1, 100, 7000, is_bot=True,
                 ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1)
    sell = _order(2, 100, 6500, is_bot=False,
                  ts=datetime(2026, 6, 10, tzinfo=timezone.utc), tracking=2)
    rep = await pr.build_fee_report(_fake_db([buy, sell], [(_CUST, _AGENT)]))
    assert rep.buy_rows[0].realized_profit == Decimal("-50000")
    assert rep.buy_rows[0].fee == Decimal("0")  # no fee on a loss
    assert rep.per_customer[_CUST].total_fee == Decimal("0")


# ---------------------------------------------------------------------------
# get_fee_percent resolution: customer → agent → global → default (#116)
# ---------------------------------------------------------------------------


async def test_get_fee_percent_customer_override_wins():
    from types import SimpleNamespace
    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(fee_percent=Decimal("2.5")))
    assert await _REAL_GET_FEE_PERCENT(db, _AGENT, customer_id=_CUST) == Decimal("2.5")


async def test_get_fee_percent_falls_back_to_agent():
    from types import SimpleNamespace
    db = MagicMock()
    db.get = AsyncMock(side_effect=[
        SimpleNamespace(fee_percent=None),          # customer: no override
        SimpleNamespace(fee_percent=Decimal("1.5")),  # agent config
    ])
    assert await _REAL_GET_FEE_PERCENT(db, _AGENT, customer_id=_CUST) == Decimal("1.5")
