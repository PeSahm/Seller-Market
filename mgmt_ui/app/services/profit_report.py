"""Build the per-buy profit/fee report from stored ``broker_orders``.

Glues the pure FIFO matcher (:mod:`app.services.profit_matching`) to the DB:
loads fully-executed orders, classifies which BUYs were the bot's, FIFO-matches
them against the account's SELLs per (customer, isin), and rolls the matched
lots up into ONE ROW PER BUY — the shape the owner asked for in the Excel
report ("one row per successful buy with its matched/possible sell and the
realized fee").

Fee resolution is layered: a per-agent ``agent_fee_configs`` override beats the
global ``profit_fee_percent`` setting, which beats a hardcoded default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.broker_orders import BrokerOrder
from app.models.customers import Customer
from app.models.fees import AgentFeeConfig
from app.services import settings_store
from app.services.broker_orders import in_time_window
from app.services.profit_matching import OrderLeg, match_lots

logger = logging.getLogger(__name__)

_DEFAULT_FEE_PERCENT = Decimal("1.0")

# Buy is fully sold / partly sold / not sold yet ("possible sell" still to come).
STATUS_REALIZED = "realized"
STATUS_PARTIAL = "partial"
STATUS_OPEN = "open"


@dataclass
class BuyFeeRow:
    """One bot BUY with its matched sells rolled up + the fee it earns."""

    buy: BrokerOrder
    matched_volume: int = 0
    open_volume: int = 0
    buy_value: Decimal = Decimal("0")  # buy_price * matched_volume
    sell_value: Decimal = Decimal("0")  # Σ sell_price * matched qty
    realized_profit: Decimal = Decimal("0")  # sell_value − buy_value
    fee: Decimal = Decimal("0")  # fee_percent% of realized_profit (if > 0)
    fee_percent: Decimal = Decimal("0")
    last_sell_at: Optional[datetime] = None
    sell_trackings: list[int] = field(default_factory=list)
    status: str = STATUS_OPEN


@dataclass
class AgentTotals:
    agent_id: Optional[UUID]
    num_buys: int = 0
    total_buy_value: Decimal = Decimal("0")  # matched buy value
    realized_profit: Decimal = Decimal("0")
    total_fee: Decimal = Decimal("0")
    open_volume: int = 0


@dataclass
class FeeReport:
    buy_rows: list[BuyFeeRow] = field(default_factory=list)
    per_agent: dict[Optional[UUID], AgentTotals] = field(default_factory=dict)
    grand_realized: Decimal = Decimal("0")
    grand_fee: Decimal = Decimal("0")
    unmatched_sell_qty: int = 0


async def get_fee_percent(db: AsyncSession, agent_id: Optional[UUID]) -> Decimal:
    """Resolve the fee % for an agent: per-agent override → global → default.

    Returns a PERCENT (e.g. ``Decimal("1.5")`` == 1.5%).
    """
    if agent_id is not None:
        cfg = await db.get(AgentFeeConfig, agent_id)
        if cfg is not None and cfg.fee_percent is not None:
            return Decimal(cfg.fee_percent)
    try:
        return Decimal(str(await settings_store.get_setting(db, "profit_fee_percent")))
    except Exception:  # noqa: BLE001 — malformed setting shouldn't 500 the report
        logger.warning("profit_fee_percent setting is unparseable; using default")
        return _DEFAULT_FEE_PERCENT


def _leg(order: BrokerOrder) -> OrderLeg:
    """Project a BrokerOrder into the matcher's minimal leg.

    FIFO ordering uses execution_date when present (when the fill actually
    happened) else the placement time, so a buy placed at open but filled
    later still orders before a same-day sell.
    """
    ts = order.execution_date or order.created_at_broker or order.placed_at
    if ts is None:
        ts = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return OrderLeg(
        tracking_number=order.tracking_number,
        order_side=order.order_side,
        executed_volume=int(order.executed_volume or 0),
        price=Decimal(order.price) if order.price is not None else Decimal("0"),
        ts=ts,
    )


def _is_bot_buy(
    order: BrokerOrder, window_start: Optional[time], window_end: Optional[time]
) -> bool:
    """A buy counts as the bot's when the fire-log tagged it (authoritative)
    OR — for historical data with no fire-log — it landed in the market-open
    window."""
    if order.order_side != 1:
        return False
    if order.is_bot:
        return True
    return in_time_window(order, window_start, window_end)


async def build_fee_report(
    db: AsyncSession,
    *,
    agent_id: Optional[UUID] = None,
    customer_id: Optional[UUID] = None,
    broker: Optional[str] = None,
    since: Optional[date] = None,
    until: Optional[date] = None,
    window_start: Optional[time] = None,
    window_end: Optional[time] = None,
    max_rows: int = 20000,
) -> FeeReport:
    """Compute per-buy profit + operator fee across the filtered orders.

    Loads fully-executed (``state==3``) orders in the filter scope, classifies
    bot buys (fire-log tag or market-open window), FIFO-matches per
    (customer, isin) against ALL sells, and rolls matched lots up per buy.
    """
    stmt = (
        select(BrokerOrder)
        .where(BrokerOrder.state == 3)
        # A NULL price would be coerced to 0 by _leg and massively inflate
        # realized profit (and the fee). A fully-executed order should always
        # carry a price; exclude any that don't rather than corrupt the math.
        .where(BrokerOrder.price.isnot(None))
        .order_by(BrokerOrder.placed_at)
        .limit(max_rows)
    )
    if agent_id is not None:
        stmt = stmt.where(BrokerOrder.agent_id == agent_id)
    if customer_id is not None:
        stmt = stmt.where(BrokerOrder.customer_id == customer_id)
    if broker:
        stmt = stmt.where(BrokerOrder.broker == broker)
    if since is not None:
        stmt = stmt.where(
            BrokerOrder.placed_at >= datetime.combine(since, time.min, tzinfo=timezone.utc)
        )
    if until is not None:
        stmt = stmt.where(
            BrokerOrder.placed_at <= datetime.combine(until, time.max, tzinfo=timezone.utc)
        )

    orders = list((await db.execute(stmt)).scalars().all())

    # Resolve each customer's CURRENT agent. broker_orders.agent_id is a
    # fetch-time snapshot; if a customer is reassigned to another agent it
    # goes stale and would misattribute the profit/fee. Always bill the
    # customer's current owner (review finding).
    cust_agent: dict[UUID, Optional[UUID]] = {}
    cust_ids_present = {o.customer_id for o in orders if o.customer_id is not None}
    if cust_ids_present:
        rows = await db.execute(
            select(Customer.id, Customer.agent_id).where(
                Customer.id.in_(cust_ids_present)
            )
        )
        cust_agent = {cid: aid for cid, aid in rows.all()}

    # Group by (customer_id, isin). A null customer_id (unassigned account)
    # groups on its own so its orders still match among themselves.
    groups: dict[tuple, list[BrokerOrder]] = {}
    for o in orders:
        groups.setdefault((o.customer_id, o.isin), []).append(o)

    by_tracking = {o.tracking_number: o for o in orders}
    report = FeeReport()
    fee_pct_cache: dict[Optional[UUID], Decimal] = {}

    for (cust_id, _isin), group in groups.items():
        agent_id_of_group = (
            cust_agent.get(cust_id) if cust_id is not None else group[0].agent_id
        )
        if agent_id_of_group not in fee_pct_cache:
            fee_pct_cache[agent_id_of_group] = await get_fee_percent(
                db, agent_id_of_group
            )
        fee_pct = fee_pct_cache[agent_id_of_group]

        bot_buys = [o for o in group if _is_bot_buy(o, window_start, window_end)]
        sells = [o for o in group if o.order_side == 2]
        if not bot_buys:
            continue

        summary = match_lots(
            buys=[_leg(o) for o in bot_buys],
            sells=[_leg(o) for o in sells],
            fee_pct=fee_pct,
        )
        report.unmatched_sell_qty += summary.unmatched_sell_qty

        # Roll matched lots up per buy.
        per_buy: dict[int, BuyFeeRow] = {}
        for o in bot_buys:
            per_buy[o.tracking_number] = BuyFeeRow(
                buy=o,
                open_volume=int(o.executed_volume or 0),
                fee_percent=fee_pct,
            )
        for lot in summary.matched:
            row = per_buy[lot.buy_tracking]
            row.matched_volume += lot.matched_volume
            row.open_volume -= lot.matched_volume
            row.buy_value += lot.buy_price * lot.matched_volume
            row.sell_value += lot.sell_price * lot.matched_volume
            row.realized_profit += lot.realized_profit
            row.sell_trackings.append(lot.sell_tracking)
            sell_order = by_tracking.get(lot.sell_tracking)
            if sell_order is not None:
                sell_ts = (
                    sell_order.execution_date
                    or sell_order.created_at_broker
                    or sell_order.placed_at
                )
                if sell_ts is not None and (
                    row.last_sell_at is None or sell_ts > row.last_sell_at
                ):
                    row.last_sell_at = sell_ts

        for row in per_buy.values():
            if row.matched_volume == 0:
                row.status = STATUS_OPEN
            elif row.open_volume > 0:
                row.status = STATUS_PARTIAL
            else:
                row.status = STATUS_REALIZED
            # Fee only on positive realized profit (default basis).
            if row.realized_profit > 0:
                row.fee = (fee_pct / Decimal("100")) * row.realized_profit
            report.buy_rows.append(row)

            totals = report.per_agent.setdefault(
                agent_id_of_group, AgentTotals(agent_id=agent_id_of_group)
            )
            totals.num_buys += 1
            totals.total_buy_value += row.buy_value
            totals.realized_profit += row.realized_profit
            totals.total_fee += row.fee
            totals.open_volume += row.open_volume

    report.buy_rows.sort(
        key=lambda r: (r.buy.placed_at or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    report.grand_realized = sum(
        (t.realized_profit for t in report.per_agent.values()), Decimal("0")
    )
    report.grand_fee = sum(
        (t.total_fee for t in report.per_agent.values()), Decimal("0")
    )
    return report


__all__ = [
    "BuyFeeRow",
    "AgentTotals",
    "FeeReport",
    "get_fee_percent",
    "build_fee_report",
    "STATUS_REALIZED",
    "STATUS_PARTIAL",
    "STATUS_OPEN",
]
