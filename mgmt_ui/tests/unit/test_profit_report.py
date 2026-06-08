"""Engine tests for the sell-side fee report (#111).

A wrong number here is a wrong invoice, so the two fee sources are pinned
explicitly: X% of each BOT SELL's value, and a 20-day mark-to-market on UNSOLD
bot buys at today's live price. The DB query and the market-data sidecar are
mocked; ``get_fee_percent`` is stubbed to a flat 1% so the arithmetic is exact.
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
_TODAY = date(2026, 6, 30)


def _order(side, qty, price, *, is_bot, ts, tracking, isin="IRO1XXXX0001"):
    return BrokerOrder(
        customer_id=_CUST, agent_id=_AGENT, broker="ayandeh",
        account_username="4580090306", tracking_number=tracking,
        isin=isin, symbol="نماد", order_side=side,
        price=Decimal(str(price)), volume=qty, executed_volume=qty,
        state=3, is_done=True, is_bot=is_bot,
        placed_at=ts, execution_date=ts, raw_json={},
    )


def _fake_db(orders, cust_agent_rows=None):
    if cust_agent_rows is None:
        cust_agent_rows = [(_CUST, _AGENT)]

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
    async def _fee(_db, _agent):
        return Decimal("1.0")  # 1%
    monkeypatch.setattr(pr, "get_fee_percent", _fee)

    async def _price(_db, _isin):
        return 7000  # today's live price
    monkeypatch.setattr(pr.market_data_client, "get_last_price", _price)


async def test_bot_sell_fee_is_pct_of_sell_value():
    # 100 @ 6500, no buys → fee = 1% × (6500 × 100) = 6500.
    sell = _order(2, 100, 6500, is_bot=True,
                  ts=datetime(2026, 6, 20, tzinfo=timezone.utc), tracking=1)
    rep = await pr.build_fee_report(_fake_db([sell]), today=_TODAY)
    assert len(rep.rows) == 1
    r = rep.rows[0]
    assert r.kind == pr.KIND_SELL
    assert r.value == Decimal("650000") and r.fee == Decimal("6500")
    assert rep.grand_fee == Decimal("6500")
    assert rep.per_customer[_CUST].total_fee == Decimal("6500")
    assert rep.per_agent[_AGENT].total_fee == Decimal("6500")


async def test_manual_sell_earns_no_fee():
    sell = _order(2, 100, 6500, is_bot=False,
                  ts=datetime(2026, 6, 20, tzinfo=timezone.utc), tracking=1)
    rep = await pr.build_fee_report(_fake_db([sell]), today=_TODAY)
    assert rep.rows == [] and rep.grand_fee == Decimal("0")


async def test_unsold_bot_buy_over_20d_marks_to_market():
    # bot buy 100 @ 6000 placed 29 days ago, no sells, today price 7000.
    buy = _order(1, 100, 6000, is_bot=True,
                 ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1)
    rep = await pr.build_fee_report(_fake_db([buy]), today=_TODAY)
    assert len(rep.rows) == 1
    r = rep.rows[0]
    assert r.kind == pr.KIND_VIRTUAL and r.qty == 100
    assert r.price == Decimal("7000") and r.value == Decimal("700000")
    assert r.fee == Decimal("7000") and r.age_days == 29


async def test_unsold_bot_buy_under_20d_no_fee():
    buy = _order(1, 100, 6000, is_bot=True,
                 ts=datetime(2026, 6, 25, tzinfo=timezone.utc), tracking=1)  # 5d
    rep = await pr.build_fee_report(_fake_db([buy]), today=_TODAY)
    assert rep.rows == []


async def test_partial_sell_then_virtual_on_remainder():
    buy = _order(1, 100, 6000, is_bot=True,
                 ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1)   # 29d
    sell = _order(2, 60, 6500, is_bot=True,
                  ts=datetime(2026, 6, 20, tzinfo=timezone.utc), tracking=2)
    rep = await pr.build_fee_report(_fake_db([buy, sell]), today=_TODAY)
    assert sorted(r.kind for r in rep.rows) == [pr.KIND_SELL, pr.KIND_VIRTUAL]
    sell_row = next(r for r in rep.rows if r.kind == pr.KIND_SELL)
    virt_row = next(r for r in rep.rows if r.kind == pr.KIND_VIRTUAL)
    assert sell_row.fee == Decimal("3900")          # 1% × 60 × 6500
    assert virt_row.qty == 40 and virt_row.fee == Decimal("2800")  # 1% × 40 × 7000
    assert rep.grand_fee == Decimal("6700")


async def test_manual_sell_consumes_bot_buy_no_fee_either_way():
    # A MANUAL sell fully exits a bot buy → no sell fee (manual) AND no virtual
    # (the lot is sold). The operator earns nothing on a manually-exited bot buy.
    buy = _order(1, 100, 6000, is_bot=True,
                 ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1)
    manual_sell = _order(2, 100, 6500, is_bot=False,
                         ts=datetime(2026, 6, 20, tzinfo=timezone.utc), tracking=2)
    rep = await pr.build_fee_report(_fake_db([buy, manual_sell]), today=_TODAY)
    assert rep.rows == [] and rep.grand_fee == Decimal("0")


async def test_virtual_skipped_when_no_live_price(monkeypatch):
    async def _none(_db, _isin):
        return None
    monkeypatch.setattr(pr.market_data_client, "get_last_price", _none)
    buy = _order(1, 100, 6000, is_bot=True,
                 ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1)
    rep = await pr.build_fee_report(_fake_db([buy]), today=_TODAY)
    assert rep.rows == []  # no price → no virtual sell, no fee
