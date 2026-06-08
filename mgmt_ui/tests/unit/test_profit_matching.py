"""Unit tests for ``app.services.profit_matching`` — the money-critical core.

The matcher pairs the bot's executed BUY lots against later SELLs (FIFO) per
(customer, isin) and computes realized profit + the operator's profit-share
fee. Everything is :class:`~decimal.Decimal` (Iranian Rial values are huge —
never float). Wrong matching = wrong invoices, so the edge-case matrix here is
deliberately exhaustive.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.services.profit_matching import (
    OrderLeg,
    compute_open_lots,
    match_lots,
)


def _buy(tracking, qty, price, *, minute=0):
    return OrderLeg(
        tracking_number=tracking,
        order_side=1,
        executed_volume=qty,
        price=Decimal(str(price)),
        ts=datetime(2026, 6, 1, 8, 45, minute, tzinfo=timezone.utc),
    )


def _sell(tracking, qty, price, *, day=2, minute=0):
    return OrderLeg(
        tracking_number=tracking,
        order_side=2,
        executed_volume=qty,
        price=Decimal(str(price)),
        ts=datetime(2026, 6, day, 10, 0, minute, tzinfo=timezone.utc),
    )


def test_single_buy_single_sell_full_match_positive_profit():
    """One buy fully matched by one sell at a higher price → positive profit,
    fee = pct% of profit, no open/unmatched remainder."""
    summary = match_lots(
        buys=[_buy(1, 100, 10)],
        sells=[_sell(2, 100, 15)],
        fee_pct=Decimal("10"),  # 10%
    )
    assert len(summary.matched) == 1
    lot = summary.matched[0]
    assert lot.buy_tracking == 1
    assert lot.sell_tracking == 2
    assert lot.matched_volume == 100
    assert lot.realized_profit == Decimal("500")  # (15-10)*100
    assert summary.realized_total == Decimal("500")
    assert summary.realized_positive == Decimal("500")
    assert summary.fee_on_positive == Decimal("50")  # 10% of 500
    assert summary.fee_on_net == Decimal("50")
    assert summary.open_position_qty == 0
    assert summary.unmatched_sell_qty == 0


def test_partial_sell_leaves_open_position():
    """Bot bought 100, sold only 40 → 60 remain open (no fee on open qty)."""
    summary = match_lots(
        buys=[_buy(1, 100, 10)],
        sells=[_sell(2, 40, 15)],
        fee_pct=Decimal("10"),
    )
    assert summary.matched[0].matched_volume == 40
    assert summary.matched[0].realized_profit == Decimal("200")  # (15-10)*40
    assert summary.open_position_qty == 60
    assert summary.unmatched_sell_qty == 0
    assert summary.fee_on_positive == Decimal("20")


def test_fifo_one_sell_consumes_multiple_buys():
    """A single sell consumes the OLDEST buy lots first (FIFO), splitting as
    needed; profit is computed per lot at each lot's own buy price."""
    summary = match_lots(
        buys=[_buy(1, 60, 10, minute=0), _buy(2, 60, 12, minute=1)],
        sells=[_sell(3, 100, 15)],
        fee_pct=Decimal("10"),
    )
    assert len(summary.matched) == 2
    # First 60 from buy#1 @10, next 40 from buy#2 @12.
    assert summary.matched[0].buy_tracking == 1
    assert summary.matched[0].matched_volume == 60
    assert summary.matched[0].realized_profit == Decimal("300")  # (15-10)*60
    assert summary.matched[1].buy_tracking == 2
    assert summary.matched[1].matched_volume == 40
    assert summary.matched[1].realized_profit == Decimal("120")  # (15-12)*40
    assert summary.realized_total == Decimal("420")
    assert summary.open_position_qty == 20  # 120 bought, 100 sold
    assert summary.unmatched_sell_qty == 0


def test_over_sell_excess_is_unmatched_not_fee_bearing():
    """Selling more than the bot bought (customer holds pre-existing shares):
    the excess sell volume is unmatched and earns NO fee."""
    summary = match_lots(
        buys=[_buy(1, 50, 10)],
        sells=[_sell(2, 80, 15)],
        fee_pct=Decimal("10"),
    )
    assert summary.matched[0].matched_volume == 50
    assert summary.unmatched_sell_qty == 30
    assert summary.open_position_qty == 0
    assert summary.realized_total == Decimal("250")  # (15-10)*50


def test_sell_with_no_buy_is_all_unmatched():
    """A sell with no bot buy to match (e.g. pre-2025-11 holding) contributes
    nothing to profit/fee and surfaces as unmatched sell volume."""
    summary = match_lots(
        buys=[],
        sells=[_sell(2, 100, 15)],
        fee_pct=Decimal("10"),
    )
    assert summary.matched == []
    assert summary.unmatched_sell_qty == 100
    assert summary.realized_total == Decimal("0")
    assert summary.fee_on_positive == Decimal("0")


def test_loss_lot_excluded_from_positive_fee_but_in_net():
    """A losing lot (sell < buy) yields negative realized: it's EXCLUDED from
    the positive-only fee but reduces the net total."""
    summary = match_lots(
        buys=[_buy(1, 100, 20, minute=0), _buy(2, 100, 10, minute=1)],
        sells=[_sell(3, 100, 15, minute=0), _sell(4, 100, 15, minute=1)],
        fee_pct=Decimal("10"),
    )
    # FIFO: sell#3(100@15) vs buy#1(100@20) -> -500 (loss);
    #       sell#4(100@15) vs buy#2(100@10) -> +500 (gain).
    assert summary.matched[0].realized_profit == Decimal("-500")
    assert summary.matched[1].realized_profit == Decimal("500")
    assert summary.realized_total == Decimal("0")
    assert summary.realized_positive == Decimal("500")
    assert summary.fee_on_positive == Decimal("50")  # 10% of the +500 lot only
    assert summary.fee_on_net == Decimal("0")  # 10% of max(net=0, 0)


def test_open_position_only_no_sells():
    """Bot bought, nothing sold yet → all open, zero realized, zero fee."""
    summary = match_lots(
        buys=[_buy(1, 100, 10), _buy(2, 50, 12)],
        sells=[],
        fee_pct=Decimal("10"),
    )
    assert summary.matched == []
    assert summary.open_position_qty == 150
    assert summary.realized_total == Decimal("0")
    assert summary.fee_on_positive == Decimal("0")


def test_empty_inputs_all_zero():
    summary = match_lots(buys=[], sells=[], fee_pct=Decimal("10"))
    assert summary.matched == []
    assert summary.open_position_qty == 0
    assert summary.unmatched_sell_qty == 0
    assert summary.realized_total == Decimal("0")
    assert summary.fee_on_positive == Decimal("0")
    assert summary.fee_on_net == Decimal("0")


def test_fractional_percent_uses_full_precision():
    """A 1.5% fee on a non-round profit keeps full Decimal precision (no
    float drift) — the matcher does NOT round; rounding is the caller's job."""
    summary = match_lots(
        buys=[_buy(1, 333, 1003)],
        sells=[_sell(2, 333, 1007)],
        fee_pct=Decimal("1.5"),
    )
    profit = Decimal("4") * 333  # (1007-1003)*333 = 1332
    assert summary.realized_total == profit
    assert summary.fee_on_positive == (Decimal("1.5") / Decimal("100")) * profit


def test_multiple_sells_drain_one_buy_fifo():
    """One big buy drained by several smaller sells in order."""
    summary = match_lots(
        buys=[_buy(1, 100, 10)],
        sells=[_sell(2, 30, 15, minute=0), _sell(3, 70, 20, minute=1)],
        fee_pct=Decimal("10"),
    )
    assert len(summary.matched) == 2
    assert summary.matched[0].matched_volume == 30
    assert summary.matched[0].realized_profit == Decimal("150")  # (15-10)*30
    assert summary.matched[1].matched_volume == 70
    assert summary.matched[1].realized_profit == Decimal("700")  # (20-10)*70
    assert summary.open_position_qty == 0
    assert summary.realized_total == Decimal("850")


def test_zero_executed_volume_buys_ignored():
    """A 'buy' with zero executed volume (placed but unfilled) is not a lot."""
    summary = match_lots(
        buys=[_buy(1, 0, 10), _buy(2, 100, 10)],
        sells=[_sell(3, 100, 15)],
        fee_pct=Decimal("10"),
    )
    assert len(summary.matched) == 1
    assert summary.matched[0].buy_tracking == 2
    assert summary.open_position_qty == 0


# ---------------------------------------------------------------------------
# compute_open_lots — the unsold remainder driving the 20-day rule (#111)
# ---------------------------------------------------------------------------


def test_open_lots_no_sells_returns_all_buys():
    lots = compute_open_lots(buys=[_buy(1, 100, 10), _buy(2, 50, 12, minute=1)], sells=[])
    assert {(l.buy_tracking, l.qty) for l in lots} == {(1, 100), (2, 50)}


def test_open_lots_fully_sold_returns_empty():
    lots = compute_open_lots(buys=[_buy(1, 100, 10)], sells=[_sell(2, 100, 15)])
    assert lots == []


def test_open_lots_partial_sell_leaves_remainder():
    lots = compute_open_lots(buys=[_buy(1, 100, 10)], sells=[_sell(2, 60, 15)])
    assert len(lots) == 1
    assert lots[0].buy_tracking == 1 and lots[0].qty == 40
    assert lots[0].buy_ts == _buy(1, 100, 10).ts  # carries the buy timestamp


def test_open_lots_fifo_consumes_oldest_first():
    # Sell 120 against two buys (100 @ t0, 80 @ t1) → first buy gone, second has 60.
    lots = compute_open_lots(
        buys=[_buy(1, 100, 10), _buy(2, 80, 11, minute=1)],
        sells=[_sell(3, 120, 15)],
    )
    assert len(lots) == 1
    assert lots[0].buy_tracking == 2 and lots[0].qty == 60


def test_open_lots_oversell_drops_everything():
    # More sold than bought → no open lots (excess sell is just ignored here).
    lots = compute_open_lots(buys=[_buy(1, 100, 10)], sells=[_sell(2, 150, 15)])
    assert lots == []
