"""Pure FIFO buy↔sell profit matcher + operator profit-share fee.

The operator's fee is a percentage of the *profit* the bot generates for an
account: for each successful BUY the bot placed, we match the later SELL of
the same stock and bill X% of ``sell_value − buy_value``. This module is the
pure, I/O-free core that does the matching and the arithmetic — it's unit
tested exhaustively (``tests/unit/test_profit_matching.py``) because a bug
here is a wrong invoice.

Conventions:

* Everything is :class:`~decimal.Decimal`. Iranian Rial values are huge;
  float would drift. The matcher NEVER rounds — callers quantize at the
  display/billing edge.
* Profit basis is GROSS (price difference only). The product owner said the
  fee is "not related to brokers", so broker ``total_fee`` is NOT subtracted.
* The fee is reported two ways: ``fee_on_positive`` (X% of the profitable
  lots only — the default billing basis) and ``fee_on_net`` (X% of the net,
  floored at zero so a losing period never bills a negative fee). The caller
  chooses which to bill.
* Only BOT-attributed buys should be passed in as ``buys`` (the caller filters
  on :attr:`BrokerOrder.is_bot`); sells are passed regardless of who placed
  them, since any sell can realize a bot-bought lot.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class OrderLeg:
    """The minimal slice of a broker order the matcher needs.

    Built from a :class:`app.models.broker_orders.BrokerOrder` row (or
    constructed directly in tests). ``ts`` drives FIFO ordering — use the
    execution/placement time; ties break on ``tracking_number`` for
    determinism.
    """

    tracking_number: int
    order_side: int  # 1 = buy, 2 = sell
    executed_volume: int
    price: Decimal
    ts: datetime


@dataclass(frozen=True)
class MatchedLot:
    """One FIFO pairing of a buy lot slice against a sell."""

    buy_tracking: int
    sell_tracking: int
    matched_volume: int
    buy_price: Decimal
    sell_price: Decimal
    realized_profit: Decimal  # (sell_price - buy_price) * matched_volume


@dataclass
class MatchSummary:
    """Aggregate result of matching one (customer, isin)'s buys and sells."""

    matched: list[MatchedLot] = field(default_factory=list)
    open_position_qty: int = 0  # bot bought, not yet sold — no fee until sold
    unmatched_sell_qty: int = 0  # sold w/o a bot buy (pre-existing) — no fee
    realized_total: Decimal = Decimal("0")  # net of gains AND losses
    realized_positive: Decimal = Decimal("0")  # gains only
    fee_on_positive: Decimal = Decimal("0")  # X% of realized_positive
    fee_on_net: Decimal = Decimal("0")  # X% of max(realized_total, 0)

    @property
    def matched_volume(self) -> int:
        return sum(lot.matched_volume for lot in self.matched)


def match_lots(
    *,
    buys: list[OrderLeg],
    sells: list[OrderLeg],
    fee_pct: Decimal,
) -> MatchSummary:
    """FIFO-match ``sells`` against ``buys`` CHRONOLOGICALLY and compute the
    profit-share fee.

    ``fee_pct`` is a PERCENT (e.g. ``Decimal("1.5")`` == 1.5%). Buys with zero
    executed volume are ignored (placed but unfilled). Events are walked in
    time order: a buy opens a lot, a sell consumes the oldest lots **bought
    before it** — a sell can never close a buy that happens later (a real
    portfolio can't; the old all-buys-up-front queue let a June-3 sell "close"
    a June-10 buy, fabricating realized profit/loss that never happened).
    At equal timestamps the buy sorts first, so a same-second fill pair still
    matches. Sell volume with no earlier open lot is pre-existing/manual
    holdings → ``unmatched_sell_qty``; buy volume never sold becomes
    ``open_position_qty``.
    """
    summary = MatchSummary()

    # FIFO queue of open buy lots AS OF the current event: [qty, price, tracking].
    open_lots: deque[list] = deque()

    events = sorted(
        [b for b in buys if b.executed_volume > 0] + list(sells),
        key=lambda o: (o.ts, o.order_side, o.tracking_number),
    )
    for leg in events:
        if leg.order_side == 1:
            open_lots.append([leg.executed_volume, leg.price, leg.tracking_number])
            continue
        sell = leg
        remaining = sell.executed_volume
        while remaining > 0 and open_lots:
            lot = open_lots[0]
            lot_qty, buy_price, buy_tracking = lot
            take = min(lot_qty, remaining)
            realized = (sell.price - buy_price) * take
            summary.matched.append(
                MatchedLot(
                    buy_tracking=buy_tracking,
                    sell_tracking=sell.tracking_number,
                    matched_volume=take,
                    buy_price=buy_price,
                    sell_price=sell.price,
                    realized_profit=realized,
                )
            )
            remaining -= take
            if take == lot_qty:
                open_lots.popleft()
            else:
                lot[0] = lot_qty - take
        if remaining > 0:
            summary.unmatched_sell_qty += remaining

    summary.open_position_qty = sum(lot[0] for lot in open_lots)
    summary.realized_total = sum(
        (lot.realized_profit for lot in summary.matched), Decimal("0")
    )
    summary.realized_positive = sum(
        (lot.realized_profit for lot in summary.matched if lot.realized_profit > 0),
        Decimal("0"),
    )
    rate = fee_pct / Decimal("100")
    summary.fee_on_positive = rate * summary.realized_positive
    summary.fee_on_net = rate * max(summary.realized_total, Decimal("0"))
    return summary


__all__ = ["OrderLeg", "MatchedLot", "MatchSummary", "match_lots"]
