"""Engine tests for the buy-side fee report + per-customer rollup (#116).

Fee = X% of the positive realized profit on bot buys (matched against ALL later
sells, manual or bot). The report also rolls up per customer (owed − paid =
remaining) and lists EVERY customer with orders in scope (show-all). The DB
query, the fee resolver, and the payments ledger are mocked.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.broker_orders import BrokerOrder
from app.services import profit_report as pr

_AGENT = uuid.uuid4()
_CUST = uuid.uuid4()
_CUST_B = uuid.uuid4()
_TODAY = date(2026, 6, 30)

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

    # No live price by default → the 20-day pass is a no-op unless a test opts in.
    async def _price(_db, _isin):
        return None
    monkeypatch.setattr(pr.market_data_client, "get_last_price", _price)

    async def _loss(_db, _agent):
        return Decimal("0")
    monkeypatch.setattr(pr, "get_loss_fee_rial", _loss)


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
# 20-day mark-to-market on unsold positions
# ---------------------------------------------------------------------------

_OLD = datetime(2026, 6, 1, tzinfo=timezone.utc)   # 29 days before _TODAY
_RECENT = datetime(2026, 6, 25, tzinfo=timezone.utc)  # 5 days before _TODAY


async def test_20day_profit_bills_pct_of_paper_gain(monkeypatch):
    async def _price(_db, _isin):
        return 7000
    monkeypatch.setattr(pr.market_data_client, "get_last_price", _price)
    # bot buy 100 @ 6000 placed 29d ago, NEVER sold → open 100, today 7000.
    buy = _order(1, 100, 6000, is_bot=True, ts=_OLD, tracking=1)
    rep = await pr.build_fee_report(_fake_db([buy], [(_CUST, _AGENT)]), today=_TODAY)
    assert len(rep.virtual_rows) == 1
    v = rep.virtual_rows[0]
    assert v.trigger == "20d" and v.in_loss is False and v.open_qty == 100
    assert v.fee == Decimal("1000")  # 1% × (7000-6000) × 100 = 1% × 100000
    assert rep.per_customer[_CUST].mark_fee == Decimal("1000")
    assert rep.per_customer[_CUST].total_fee == Decimal("1000")
    assert rep.grand_fee == Decimal("1000")


async def test_20day_loss_bills_fixed_per_agent_fee(monkeypatch):
    async def _price(_db, _isin):
        return 5500  # below the 6000 buy → loss
    monkeypatch.setattr(pr.market_data_client, "get_last_price", _price)

    async def _loss(_db, _agent):
        return Decimal("500000")  # fixed loss fee, in Rial
    monkeypatch.setattr(pr, "get_loss_fee_rial", _loss)

    buy = _order(1, 100, 6000, is_bot=True, ts=_OLD, tracking=1)
    rep = await pr.build_fee_report(_fake_db([buy], [(_CUST, _AGENT)]), today=_TODAY)
    assert len(rep.virtual_rows) == 1
    v = rep.virtual_rows[0]
    assert v.in_loss is True and v.fee == Decimal("500000")
    assert rep.per_customer[_CUST].total_fee == Decimal("500000")


async def test_20day_skips_recent_lots(monkeypatch):
    async def _price(_db, _isin):
        return 7000
    monkeypatch.setattr(pr.market_data_client, "get_last_price", _price)
    buy = _order(1, 100, 6000, is_bot=True, ts=_RECENT, tracking=1)  # only 5 days old
    rep = await pr.build_fee_report(_fake_db([buy], [(_CUST, _AGENT)]), today=_TODAY)
    assert rep.virtual_rows == []


async def test_20day_skips_when_no_price():
    # fixture's get_last_price returns None → no mark-to-market.
    buy = _order(1, 100, 6000, is_bot=True, ts=_OLD, tracking=1)
    rep = await pr.build_fee_report(_fake_db([buy], [(_CUST, _AGENT)]), today=_TODAY)
    assert rep.virtual_rows == []


# ---------------------------------------------------------------------------
# Whole-position realization on the FIRST sell (at the avg sell price)
# ---------------------------------------------------------------------------


async def test_sell_realizes_whole_remainder_at_sell_price():
    # buy 100 @ 6000 (recent), customer sells only 60 @ 6500. The sold 60 are
    # realized via FIFO (fee 300); the unsold 40 are realized at the avg sell
    # price 6500 (fee 200) — total 500 = the whole position at 6500.
    buy = _order(1, 100, 6000, is_bot=True, ts=_RECENT, tracking=1)
    sell = _order(2, 60, 6500, is_bot=False, ts=_RECENT, tracking=2)
    rep = await pr.build_fee_report(_fake_db([buy, sell], [(_CUST, _AGENT)]), today=_TODAY)
    # FIFO realized on the sold 60.
    assert rep.buy_rows[0].fee == Decimal("300")  # 1% × (6500-6000) × 60
    # Remainder realized on the sell trigger.
    assert len(rep.virtual_rows) == 1
    v = rep.virtual_rows[0]
    assert v.trigger == "sell" and v.in_loss is False and v.open_qty == 40
    assert v.fee == Decimal("200")  # 1% × (6500-6000) × 40
    assert rep.per_customer[_CUST].total_fee == Decimal("500")  # 300 + 200


async def test_sell_at_loss_remainder_uses_fixed_fee(monkeypatch):
    async def _loss(_db, _agent):
        return Decimal("300000")  # fixed loss fee (Rial)
    monkeypatch.setattr(pr, "get_loss_fee_rial", _loss)
    # buy 100 @ 7000 (recent), sells 1 @ 6500 (a loss). Sold 1 realizes nothing
    # (loss → no positive fee); the unsold 99 at 6500 < 7000 → fixed loss fee.
    buy = _order(1, 100, 7000, is_bot=True, ts=_RECENT, tracking=1)
    sell = _order(2, 1, 6500, is_bot=False, ts=_RECENT, tracking=2)
    rep = await pr.build_fee_report(_fake_db([buy, sell], [(_CUST, _AGENT)]), today=_TODAY)
    assert rep.buy_rows[0].fee == Decimal("0")  # the 1 sold share was a loss
    v = rep.virtual_rows[0]
    assert v.trigger == "sell" and v.in_loss is True and v.fee == Decimal("300000")
    assert rep.per_customer[_CUST].total_fee == Decimal("300000")


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
