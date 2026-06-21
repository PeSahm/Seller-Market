"""Build the per-buy profit/fee report from stored ``broker_orders``.

The operator's fee is a percentage of the *profit* the bot generates: for each
bot BUY (identified by the fire-log tag OR the market-open window), FIFO-match
the later SELLs of the same stock and bill X% of the positive realized profit
(``sell_value − buy_value``). This is the model the operator relies on.

On top of the per-buy detail this also rolls up **per customer** — owed
(computed) − paid (the received-fee ledger) = remaining — and resolves the fee %
per **customer → agent → global → default** (#116). Every customer with bot
orders in scope appears (even when they owe nothing) so the operator can always
reach each one's fee-% config + payment form.
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
from app.services import close_prices as close_prices_svc
from app.services import fee_payments, settings_store
from app.services.broker_orders import in_time_window, is_excluded
from app.services.profit_matching import OrderLeg, match_lots

logger = logging.getLogger(__name__)

_DEFAULT_FEE_PERCENT = Decimal("1.0")
_TOMAN_TO_RIAL = Decimal("10")

# Buy is fully sold / partly sold / not sold yet ("possible sell" still to come).
STATUS_REALIZED = "realized"
STATUS_PARTIAL = "partial"
STATUS_OPEN = "open"


@dataclass
class MatchedSell:
    """One sell slice that realized part of a bot buy — for the fee sub-grid.

    ``sell`` is the actual sell order (resolved GROUP-LOCALLY, not via the global
    by-tracking map — tracking_number is a broker-day sequence, not globally
    unique). It can be ``None`` only for malformed data; templates guard.
    """

    sell: Optional[BrokerOrder]
    matched_volume: int
    sell_price: Decimal
    sell_value: Decimal  # sell_price * matched_volume
    realized_profit: Decimal  # (sell_price − buy_price) * matched_volume
    sell_at: Optional[datetime] = None


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
    # The sells (with details) that realized this buy — drives the UI sub-grid.
    matched_sells: list[MatchedSell] = field(default_factory=list)
    status: str = STATUS_OPEN


@dataclass
class VirtualFeeRow:
    """Manual close of the UNSOLD remainder of a bot-bought position.

    When an ISIN has a saved global CLOSE PRICE (set on the Close-positions
    page — replacing the old automatic 20-day rule), the whole open remainder
    of each (customer, isin) group is realized at that price — regardless of
    partial sells (the sold shares already billed via FIFO in the per-buy rows).
    In profit → fee = X% of the gain; in loss → a fixed per-agent fee;
    break-even → no fee. Recomputed live each view: editing the close price
    re-adjusts, clearing it re-opens the position (this row disappears).
    """

    customer_id: Optional[UUID]
    agent_id: Optional[UUID]
    broker: str
    isin: str
    symbol: str
    open_qty: int
    avg_buy_price: Decimal
    price: int          # realization price (the saved close price)
    trigger: str        # "close"
    in_loss: bool
    fee: Decimal
    oldest_buy_date: Optional[date] = None  # earliest open buy — how long held


@dataclass
class CustomerFeeTotals:
    """Per-customer rollup: owed (computed) − paid (ledger) = remaining (#116)."""

    customer_id: Optional[UUID]
    agent_id: Optional[UUID]
    num_buys: int = 0
    total_buy_value: Decimal = Decimal("0")
    realized_profit: Decimal = Decimal("0")
    total_fee: Decimal = Decimal("0")  # owed (realized + manual-close portion)
    mark_fee: Decimal = Decimal("0")   # the close-price realized portion
    open_volume: int = 0
    paid: Decimal = Decimal("0")
    remaining: Decimal = Decimal("0")


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
    virtual_rows: list[VirtualFeeRow] = field(default_factory=list)
    per_customer: dict[Optional[UUID], CustomerFeeTotals] = field(default_factory=dict)
    per_agent: dict[Optional[UUID], AgentTotals] = field(default_factory=dict)
    grand_realized: Decimal = Decimal("0")
    grand_fee: Decimal = Decimal("0")
    unmatched_sell_qty: int = 0


async def get_fee_percent(
    db: AsyncSession,
    agent_id: Optional[UUID],
    customer_id: Optional[UUID] = None,
) -> Decimal:
    """Resolve the fee %: per-customer → per-agent → global → default (#116).

    Returns a PERCENT (e.g. ``Decimal("1.5")`` == 1.5%).
    """
    if customer_id is not None:
        cust = await db.get(Customer, customer_id)
        if cust is not None and cust.fee_percent is not None:
            return Decimal(cust.fee_percent)
    if agent_id is not None:
        cfg = await db.get(AgentFeeConfig, agent_id)
        if cfg is not None and cfg.fee_percent is not None:
            return Decimal(cfg.fee_percent)
    try:
        return Decimal(str(await settings_store.get_setting(db, "profit_fee_percent")))
    except Exception:  # noqa: BLE001 — malformed setting shouldn't 500 the report
        logger.warning("profit_fee_percent setting is unparseable; using default")
        return _DEFAULT_FEE_PERCENT


async def get_loss_fee_rial(db: AsyncSession, agent_id: Optional[UUID]) -> Decimal:
    """Resolve the fixed loss-fee (in RIAL) billed on a manual close at a loss.

    Per-agent ``agent_fee_configs.loss_fee_toman`` override → global
    ``mark_to_market_loss_fee_toman`` setting → 0. Stored in Toman; returned
    ×10 as Rial so it adds straight onto the report's Rial totals.
    """
    toman: Optional[Decimal] = None
    if agent_id is not None:
        cfg = await db.get(AgentFeeConfig, agent_id)
        if cfg is not None and getattr(cfg, "loss_fee_toman", None) is not None:
            toman = Decimal(cfg.loss_fee_toman)
    if toman is None:
        try:
            toman = Decimal(
                str(await settings_store.get_setting(db, "mark_to_market_loss_fee_toman"))
            )
        except Exception:  # noqa: BLE001 — malformed setting shouldn't 500 the report
            toman = Decimal("0")
    return toman * _TOMAN_TO_RIAL


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
    exclude: Optional[set[str]] = None,
    today: Optional[date] = None,
    max_rows: int = 20000,
) -> FeeReport:
    """Compute per-buy profit + operator fee across the filtered orders.

    Loads fully-executed (``state==3``) orders in scope, classifies bot buys
    (fire-log tag or market-open window), FIFO-matches per (customer, isin)
    against ALL sells, rolls matched lots up per buy, and rolls up per customer
    (owed − paid = remaining) + per agent. Every customer with orders in scope
    appears, even at zero, so each is reachable for fee config + payments.

    Manual close: when an ISIN has a saved GLOBAL close price (set on the
    Close-positions page), the whole UNSOLD remainder of each (customer, isin)
    group is realized at that price — profit → fee = X% of the gain; loss → a
    fixed per-agent fee; break-even → no fee. No saved price → the remainder
    stays open (no fee). ``today`` is accepted for backward compatibility but no
    longer affects the result (the old time-based 20-day rule was removed).
    """
    stmt = (
        select(BrokerOrder)
        .where(BrokerOrder.state == 3)
        .where(BrokerOrder.price.isnot(None))
        # Per-order decline: a declined order ("the customer's own manual trade")
        # never enters grouping/matching, so a declined buy drops out of buy_rows
        # and a declined sell stops realizing its buy — totals auto-adjust.
        .where(BrokerOrder.fee_excluded_at.is_(None))
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
    if exclude:
        orders = [o for o in orders if not is_excluded(o, exclude)]

    # Resolve each customer's CURRENT agent (the snapshot on broker_orders goes
    # stale on reassignment — always bill the current owner).
    cust_agent: dict[UUID, Optional[UUID]] = {}
    cust_ids_present = {o.customer_id for o in orders if o.customer_id is not None}
    if cust_ids_present:
        rows = await db.execute(
            select(Customer.id, Customer.agent_id).where(
                Customer.id.in_(cust_ids_present)
            )
        )
        cust_agent = {cid: aid for cid, aid in rows.all()}

    # Saved global close prices for the instruments in scope (replaces the old
    # per-isin live-price lookup). An ISIN with a row here gets its open
    # remainder realized; absent → the remainder stays open.
    close_prices = await close_prices_svc.get_close_prices(
        db, {o.isin for o in orders if o.isin}
    )

    groups: dict[tuple, list[BrokerOrder]] = {}
    for o in orders:
        groups.setdefault((o.customer_id, o.isin), []).append(o)

    report = FeeReport()
    fee_pct_cache: dict[tuple, Decimal] = {}
    loss_fee_cache: dict[Optional[UUID], Decimal] = {}

    async def _loss_fee(agent: Optional[UUID]) -> Decimal:
        if agent not in loss_fee_cache:
            loss_fee_cache[agent] = await get_loss_fee_rial(db, agent)
        return loss_fee_cache[agent]

    for (cust_id, _isin), group in groups.items():
        agent_id_of_group = (
            cust_agent.get(cust_id) if cust_id is not None else group[0].agent_id
        )
        ck = (cust_id, agent_id_of_group)
        if ck not in fee_pct_cache:
            fee_pct_cache[ck] = await get_fee_percent(
                db, agent_id_of_group, customer_id=cust_id
            )
        fee_pct = fee_pct_cache[ck]

        # Ensure a per-customer row exists for EVERY customer with orders in
        # scope (show-all), so even a zero-fee customer is listed + reachable.
        ctot = report.per_customer.setdefault(
            cust_id, CustomerFeeTotals(customer_id=cust_id, agent_id=agent_id_of_group)
        )

        bot_buys = [o for o in group if _is_bot_buy(o, window_start, window_end)]
        sells = [o for o in group if o.order_side == 2]
        # Group-LOCAL sell lookup for the sub-grid. NOT the global by_tracking
        # (line ~296): tracking_number is a broker-day sequence (repeats across
        # days/accounts), so the global map can attach a foreign group's sell.
        sells_by_tracking = {s.tracking_number: s for s in sells}
        if not bot_buys:
            continue

        summary = match_lots(
            buys=[_leg(o) for o in bot_buys],
            sells=[_leg(o) for o in sells],
            fee_pct=fee_pct,
        )
        report.unmatched_sell_qty += summary.unmatched_sell_qty

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
            sell_order = sells_by_tracking.get(lot.sell_tracking)
            sell_ts = None
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
            row.matched_sells.append(
                MatchedSell(
                    sell=sell_order,
                    matched_volume=lot.matched_volume,
                    sell_price=lot.sell_price,
                    sell_value=lot.sell_price * lot.matched_volume,
                    realized_profit=lot.realized_profit,
                    sell_at=sell_ts,
                )
            )

        for row in per_buy.values():
            if row.matched_volume == 0:
                row.status = STATUS_OPEN
            elif row.open_volume > 0:
                row.status = STATUS_PARTIAL
            else:
                row.status = STATUS_REALIZED
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

            ctot.num_buys += 1
            ctot.total_buy_value += row.buy_value
            ctot.realized_profit += row.realized_profit
            ctot.total_fee += row.fee
            ctot.open_volume += row.open_volume

        # --- Manual close of the UNSOLD remainder (customer × stock) ---
        # Plain FIFO bills the actually-sold shares (per-buy rows above). When
        # this ISIN has a saved GLOBAL close price (set on the Close-positions
        # page — replacing the old time-based 20-day rule), the WHOLE open
        # remainder is realized at that price regardless of partial sells.
        # profit → fee = X% of the gain; loss → the fixed per-agent loss fee;
        # break-even → no fee. No saved price → the remainder stays open.
        # Recomputed live: editing the close price re-adjusts; clearing it
        # re-opens (this row disappears).
        saved = close_prices.get(_isin)
        open_lots = [
            (o, per_buy[o.tracking_number].open_volume)
            for o in bot_buys
            if per_buy[o.tracking_number].open_volume > 0
        ]
        if open_lots and saved is not None and saved > 0:
            realize_price = Decimal(saved)
            rem_qty = sum(q for _, q in open_lots)
            weighted_buy = sum(
                (
                    (Decimal(o.price) if o.price is not None else Decimal("0")) * q
                    for o, q in open_lots
                ),
                Decimal("0"),
            )
            avg_buy = weighted_buy / rem_qty
            if realize_price > avg_buy:
                vfee = (fee_pct / Decimal("100")) * (realize_price - avg_buy) * rem_qty
                in_loss = False
            elif realize_price < avg_buy:
                vfee = await _loss_fee(agent_id_of_group)
                in_loss = True
            else:
                vfee = Decimal("0")  # break-even — closed, no fee
                in_loss = False
            sym = next(
                (x.symbol or x.symbol_title for x in group if (x.symbol or x.symbol_title)),
                "",
            ) or ""
            oldest = min(
                (
                    ts.date()
                    for o, _q in open_lots
                    if (
                        ts := (
                            o.execution_date
                            or o.created_at_broker
                            or o.placed_at
                        )
                    )
                    is not None
                ),
                default=None,
            )
            # Every closed position is shown (profit, loss, or break-even) so the
            # operator sees that all open trades were calculated; only profit/loss
            # actually bill.
            report.virtual_rows.append(VirtualFeeRow(
                customer_id=cust_id, agent_id=agent_id_of_group,
                broker=group[0].broker, isin=_isin, symbol=sym,
                open_qty=rem_qty, avg_buy_price=avg_buy,
                price=int(realize_price), trigger="close",
                in_loss=in_loss, fee=vfee,
                oldest_buy_date=oldest,
            ))
            ctot.total_fee += vfee
            ctot.mark_fee += vfee
            atot = report.per_agent.setdefault(
                agent_id_of_group, AgentTotals(agent_id=agent_id_of_group)
            )
            atot.total_fee += vfee

    # Per-customer paid (received-fee ledger) → remaining = owed − paid (#116).
    paid_map = await fee_payments.total_paid_by_customer(
        db, [cid for cid in report.per_customer if cid is not None]
    )
    for cid, ct in report.per_customer.items():
        ct.paid = paid_map.get(cid, Decimal("0")) if cid is not None else Decimal("0")
        ct.remaining = ct.total_fee - ct.paid

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
    "MatchedSell",
    "VirtualFeeRow",
    "CustomerFeeTotals",
    "AgentTotals",
    "FeeReport",
    "get_fee_percent",
    "get_loss_fee_rial",
    "build_fee_report",
    "STATUS_REALIZED",
    "STATUS_PARTIAL",
    "STATUS_OPEN",
]
