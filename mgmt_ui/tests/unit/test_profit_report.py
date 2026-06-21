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

    # No saved close prices by default → open positions stay open (no fee).
    # Stubbing this means the real batch query never runs, so _fake_db's
    # 2-call execute side_effect (orders + cust_agent) is unchanged. Tests that
    # want a close realized override this with a per-isin dict.
    async def _close(_db, _isins):
        return {}
    monkeypatch.setattr(pr.close_prices_svc, "get_close_prices", _close)

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


async def test_sell_predating_bot_buy_leaves_buy_open():
    # Chronological matching: a sell EXECUTED BEFORE the bot buy closes
    # pre-existing/manual holdings, not the later buy. The buy stays fully open,
    # zero realized, zero fee. With no saved close price it produces no fee.
    sell = _order(2, 100, 6500, is_bot=False,
                  ts=datetime(2026, 6, 20, tzinfo=timezone.utc), tracking=1)
    buy = _order(1, 100, 6000, is_bot=True,
                 ts=datetime(2026, 6, 25, tzinfo=timezone.utc), tracking=2)
    rep = await pr.build_fee_report(_fake_db([sell, buy], [(_CUST, _AGENT)]))
    row = rep.buy_rows[0]
    assert row.status == "open"
    assert row.matched_volume == 0 and row.open_volume == 100
    assert row.realized_profit == Decimal("0") and row.fee == Decimal("0")
    assert rep.unmatched_sell_qty == 100
    assert rep.virtual_rows == []  # no saved close price


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
# Manual close of unsold positions (saved global close price)
# ---------------------------------------------------------------------------

_OLD = datetime(2026, 6, 1, tzinfo=timezone.utc)
_RECENT = datetime(2026, 6, 25, tzinfo=timezone.utc)
_ISIN = "IRO1XXXX0001"  # the _order() default isin


def _close_prices(mapping):
    async def _close(_db, _isins):
        return dict(mapping)
    return _close


async def test_close_realizes_open_remainder_at_saved_price(monkeypatch):
    # bot buy 100 @ 6000, never sold → open 100; saved close price 7000.
    monkeypatch.setattr(pr.close_prices_svc, "get_close_prices",
                        _close_prices({_ISIN: Decimal("7000")}))
    buy = _order(1, 100, 6000, is_bot=True, ts=_OLD, tracking=1)
    rep = await pr.build_fee_report(_fake_db([buy], [(_CUST, _AGENT)]))
    assert len(rep.virtual_rows) == 1
    v = rep.virtual_rows[0]
    assert v.trigger == "close" and v.in_loss is False and v.open_qty == 100
    assert v.fee == Decimal("1000")  # 1% × (7000-6000) × 100
    assert rep.per_customer[_CUST].mark_fee == Decimal("1000")
    assert rep.per_customer[_CUST].total_fee == Decimal("1000")
    assert rep.grand_fee == Decimal("1000")


async def test_close_loss_bills_fixed_per_agent_fee(monkeypatch):
    monkeypatch.setattr(pr.close_prices_svc, "get_close_prices",
                        _close_prices({_ISIN: Decimal("5500")}))  # below 6000 → loss

    async def _loss(_db, _agent):
        return Decimal("500000")  # fixed loss fee, in Rial
    monkeypatch.setattr(pr, "get_loss_fee_rial", _loss)

    buy = _order(1, 100, 6000, is_bot=True, ts=_OLD, tracking=1)
    rep = await pr.build_fee_report(_fake_db([buy], [(_CUST, _AGENT)]))
    assert len(rep.virtual_rows) == 1
    v = rep.virtual_rows[0]
    assert v.trigger == "close" and v.in_loss is True and v.fee == Decimal("500000")
    assert rep.per_customer[_CUST].total_fee == Decimal("500000")
    assert rep.per_customer[_CUST].mark_fee == Decimal("500000")


async def test_close_break_even_bills_no_fee(monkeypatch):
    # Close price EQUAL to the avg buy → break-even → no fee, even if a loss fee
    # is configured (the fallback price for a no-market-price symbol relies on
    # this so a forced break-even close bills nothing).
    monkeypatch.setattr(pr.close_prices_svc, "get_close_prices",
                        _close_prices({_ISIN: Decimal("6000")}))

    async def _loss(_db, _agent):
        return Decimal("500000")
    monkeypatch.setattr(pr, "get_loss_fee_rial", _loss)

    buy = _order(1, 100, 6000, is_bot=True, ts=_OLD, tracking=1)
    rep = await pr.build_fee_report(_fake_db([buy], [(_CUST, _AGENT)]))
    assert len(rep.virtual_rows) == 1  # still shown (every closed trade visible)
    v = rep.virtual_rows[0]
    assert v.in_loss is False and v.fee == Decimal("0")
    assert rep.per_customer[_CUST].total_fee == Decimal("0")
    assert rep.per_customer[_CUST].mark_fee == Decimal("0")


async def test_no_saved_price_stays_open():
    # Default fixture get_close_prices → {} → the open lot is never realized.
    buy = _order(1, 100, 6000, is_bot=True, ts=_OLD, tracking=1)
    rep = await pr.build_fee_report(_fake_db([buy], [(_CUST, _AGENT)]))
    assert rep.virtual_rows == []
    assert rep.buy_rows[0].open_volume == 100


async def test_close_has_no_time_gate(monkeypatch):
    # No aging: a RECENT buy (5 days old) with a saved close price realizes too.
    monkeypatch.setattr(pr.close_prices_svc, "get_close_prices",
                        _close_prices({_ISIN: Decimal("7000")}))
    buy = _order(1, 100, 6000, is_bot=True, ts=_RECENT, tracking=1)
    rep = await pr.build_fee_report(_fake_db([buy], [(_CUST, _AGENT)]))
    assert len(rep.virtual_rows) == 1
    assert rep.virtual_rows[0].fee == Decimal("1000")


async def test_partial_sell_then_close(monkeypatch):
    # buy 100 @ 6000, sell 60 @ 6500 (FIFO fee 300), close the unsold 40 @ 7000.
    monkeypatch.setattr(pr.close_prices_svc, "get_close_prices",
                        _close_prices({_ISIN: Decimal("7000")}))
    buy = _order(1, 100, 6000, is_bot=True, ts=_OLD, tracking=1)
    sell = _order(2, 60, 6500, is_bot=False, ts=_OLD, tracking=2)
    rep = await pr.build_fee_report(_fake_db([buy, sell], [(_CUST, _AGENT)]))
    assert rep.buy_rows[0].fee == Decimal("300")  # 1% × (6500-6000) × 60
    assert len(rep.virtual_rows) == 1
    v = rep.virtual_rows[0]
    assert v.trigger == "close" and v.open_qty == 40
    assert v.fee == Decimal("400")  # 1% × (7000-6000) × 40
    assert v.oldest_buy_date == _OLD.date()
    assert rep.per_customer[_CUST].total_fee == Decimal("700")  # 300 + 400
    assert rep.per_customer[_CUST].mark_fee == Decimal("400")


async def test_close_realizes_whole_remainder_blending_mixed_age(monkeypatch):
    # Two open lots (aged + recent) on the same customer × stock → ONE close row
    # over the WHOLE remainder at the BLENDED avg buy (no per-lot age split — the
    # whole-remainder realization the operator wants).
    monkeypatch.setattr(pr.close_prices_svc, "get_close_prices",
                        _close_prices({_ISIN: Decimal("7000")}))
    b1 = _order(1, 100, 6000, is_bot=True, ts=_OLD, tracking=1)
    b2 = _order(1, 100, 6400, is_bot=True, ts=_RECENT, tracking=2)
    rep = await pr.build_fee_report(_fake_db([b1, b2], [(_CUST, _AGENT)]))
    assert len(rep.virtual_rows) == 1
    v = rep.virtual_rows[0]
    assert v.open_qty == 200
    assert v.avg_buy_price == Decimal("6200")  # (100×6000 + 100×6400)/200
    assert v.fee == Decimal("1600")  # 1% × (7000-6200) × 200
    assert v.oldest_buy_date == _OLD.date()


async def test_fully_sold_position_has_no_close_row(monkeypatch):
    # A saved close price doesn't conjure a row when nothing is open.
    monkeypatch.setattr(pr.close_prices_svc, "get_close_prices",
                        _close_prices({_ISIN: Decimal("9000")}))
    buy = _order(1, 100, 6000, is_bot=True, ts=_OLD, tracking=1)
    sell = _order(2, 100, 6500, is_bot=False, ts=_OLD, tracking=2)
    rep = await pr.build_fee_report(_fake_db([buy, sell], [(_CUST, _AGENT)]))
    assert rep.virtual_rows == []
    assert rep.buy_rows[0].fee == Decimal("500")  # only the FIFO sell fee


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


# ---------------------------------------------------------------------------
# Per-order decline (fee_excluded_at) + matched-sell sub-grid enrichment
# ---------------------------------------------------------------------------


async def test_declined_order_absent_drops_buyrow_and_lowers_totals():
    # The decline filter lives in SQL (WHERE fee_excluded_at IS NULL); the fake
    # db can't run it, so we assert the CONSEQUENCE: with the buy filtered out
    # (only the sell loads), its fee row disappears and the totals drop to 0.
    buy = _order(1, 100, 6000, is_bot=True,
                 ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1)
    sell = _order(2, 100, 6500, is_bot=False,
                  ts=datetime(2026, 6, 10, tzinfo=timezone.utc), tracking=2)
    db_full = _fake_db([buy, sell], [(_CUST, _AGENT)])
    full = await pr.build_fee_report(db_full)
    assert len(full.buy_rows) == 1 and full.grand_fee == Decimal("500")

    declined = await pr.build_fee_report(_fake_db([sell], [(_CUST, _AGENT)]))
    assert declined.buy_rows == []
    assert declined.grand_fee == Decimal("0")
    assert declined.per_customer[_CUST].total_fee == Decimal("0")

    # Pin the contract: the orders SELECT must carry the decline predicate, so a
    # regression that drops the filter (and would re-bill declined orders) fails
    # here, not just via the consequence above.
    sql = str(db_full.execute.await_args_list[0].args[0])
    assert "fee_excluded_at" in sql and "IS NULL" in sql


async def test_matched_sells_lists_the_sell():
    buy = _order(1, 100, 6000, is_bot=True,
                 ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1)
    sell = _order(2, 100, 6500, is_bot=False,
                  ts=datetime(2026, 6, 10, tzinfo=timezone.utc), tracking=2)
    rep = await pr.build_fee_report(_fake_db([buy, sell], [(_CUST, _AGENT)]))
    row = rep.buy_rows[0]
    assert len(row.matched_sells) == 1
    ms = row.matched_sells[0]
    assert ms.matched_volume == 100
    assert ms.sell_price == Decimal("6500")
    assert ms.sell_value == Decimal("650000")
    assert ms.realized_profit == Decimal("50000")
    assert ms.sell is not None and ms.sell.tracking_number == 2


async def test_matched_sells_partial_split():
    # One buy realized by TWO sells → two MatchedSell entries with the split vols.
    buy = _order(1, 100, 6000, is_bot=True,
                 ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1)
    s1 = _order(2, 40, 6500, is_bot=False,
                ts=datetime(2026, 6, 5, tzinfo=timezone.utc), tracking=2)
    s2 = _order(2, 60, 6700, is_bot=False,
                ts=datetime(2026, 6, 9, tzinfo=timezone.utc), tracking=3)
    rep = await pr.build_fee_report(_fake_db([buy, s1, s2], [(_CUST, _AGENT)]))
    row = rep.buy_rows[0]
    vols = sorted(ms.matched_volume for ms in row.matched_sells)
    assert vols == [40, 60]
    trackings = {ms.sell.tracking_number for ms in row.matched_sells}
    assert trackings == {2, 3}


async def test_matched_sells_group_local_no_cross_group_leak():
    # Two customers (groups) REUSE tracking numbers 1/2. The group-local lookup
    # must give each buy row ITS OWN group's sell — a global by-tracking map
    # would collide on tracking=2 and attach the wrong customer's sell.
    a_buy = _order(1, 100, 6000, is_bot=True,
                   ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1, cust=_CUST)
    a_sell = _order(2, 100, 6500, is_bot=False,
                    ts=datetime(2026, 6, 10, tzinfo=timezone.utc), tracking=2, cust=_CUST)
    b_buy = _order(1, 100, 6000, is_bot=True,
                   ts=datetime(2026, 6, 1, tzinfo=timezone.utc), tracking=1, cust=_CUST_B)
    b_sell = _order(2, 100, 7000, is_bot=False,
                    ts=datetime(2026, 6, 10, tzinfo=timezone.utc), tracking=2, cust=_CUST_B)
    rep = await pr.build_fee_report(
        _fake_db([a_buy, a_sell, b_buy, b_sell], [(_CUST, _AGENT), (_CUST_B, _AGENT)])
    )
    by_cust = {r.buy.customer_id: r for r in rep.buy_rows}
    a_ms = by_cust[_CUST].matched_sells[0]
    b_ms = by_cust[_CUST_B].matched_sells[0]
    assert a_ms.sell.customer_id == _CUST and a_ms.sell_price == Decimal("6500")
    assert b_ms.sell.customer_id == _CUST_B and b_ms.sell_price == Decimal("7000")
