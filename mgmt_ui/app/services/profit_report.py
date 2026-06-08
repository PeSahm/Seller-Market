"""Build the sell-side fee report from stored ``broker_orders`` (#111 redesign).

The operator's fee model (redesigned):

* **Fee = X% of each BOT SELL's VALUE** (``sell_price × sold_qty``), charged once
  on the sell — fixed/final at sale time. Only sells the bot placed (``is_bot``)
  earn a fee; the agent's manual sells don't.
* **20-day mark-to-market on UNSOLD bot buys**: FIFO-consume every sell against
  every buy ("each buy with the first sell, for all"); any bot buy lot still
  open after >20 calendar days is **virtually sold at today's live price** (from
  the per-host market-data sidecar), and X% of that value is billed. Recomputed
  live each time the report runs — until the lot is actually sold.

So the fee comes from two row kinds: real bot **sells** and 20-day **virtual**
sells. Rows roll up per customer (owed) and per agent. ``X`` resolves per-agent
override → global ``profit_fee_percent`` → default (per-customer override is
added in #116).

Pairs the pure matcher (:mod:`app.services.profit_matching`) to the DB; a bug
here is a wrong invoice, so the engine is exhaustively unit-tested with the
sidecar price client mocked.
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
from app.services import fee_payments, market_data_client, settings_store
from app.services.broker_orders import in_time_window, is_excluded
from app.services.profit_matching import OrderLeg, compute_open_lots

logger = logging.getLogger(__name__)

_DEFAULT_FEE_PERCENT = Decimal("1.0")
_MARK_TO_MARKET_DAYS = 20

# Row kinds.
KIND_SELL = "sell"        # a real bot sell — X% of the sell value
KIND_VIRTUAL = "virtual"  # 20-day mark-to-market on an unsold bot buy

_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class FeeRow:
    """One fee-generating event — a bot sell, or a 20-day virtual sell."""

    customer_id: Optional[UUID]
    agent_id: Optional[UUID]
    broker: str
    isin: str
    symbol: str
    kind: str  # KIND_SELL | KIND_VIRTUAL
    qty: int
    price: Decimal           # sell price, or today's price for a virtual row
    value: Decimal           # price * qty
    fee_percent: Decimal
    fee: Decimal             # fee_percent% of value
    at: Optional[datetime]   # sell time, or the buy time for a virtual row
    tracking: Optional[int]  # sell tracking, or buy tracking for a virtual row
    age_days: Optional[int] = None  # virtual rows: age of the unsold buy lot


@dataclass
class CustomerFeeTotals:
    customer_id: Optional[UUID]
    agent_id: Optional[UUID]
    num_sells: int = 0
    num_virtual: int = 0
    sell_fee: Decimal = Decimal("0")
    virtual_fee: Decimal = Decimal("0")
    total_fee: Decimal = Decimal("0")  # owed
    paid: Decimal = Decimal("0")       # Σ received-fee ledger (#116)
    remaining: Decimal = Decimal("0")  # owed − paid


@dataclass
class AgentFeeTotals:
    agent_id: Optional[UUID]
    num_rows: int = 0
    total_value: Decimal = Decimal("0")
    total_fee: Decimal = Decimal("0")


@dataclass
class FeeReport:
    rows: list[FeeRow] = field(default_factory=list)
    per_customer: dict[Optional[UUID], CustomerFeeTotals] = field(default_factory=dict)
    per_agent: dict[Optional[UUID], AgentFeeTotals] = field(default_factory=dict)
    grand_value: Decimal = Decimal("0")
    grand_fee: Decimal = Decimal("0")


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


def _leg(order: BrokerOrder) -> OrderLeg:
    """Project a BrokerOrder into the matcher's minimal leg (FIFO by exec time)."""
    ts = order.execution_date or order.created_at_broker or order.placed_at
    if ts is None:
        ts = _MIN_DT
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
    """A buy is the bot's when the fire-log tagged it (authoritative) OR — for
    historical rows with no fire-log — it landed in the market-open window."""
    if order.order_side != 1:
        return False
    if order.is_bot:
        return True
    return in_time_window(order, window_start, window_end)


def _order_ts(o: BrokerOrder) -> Optional[datetime]:
    return o.execution_date or o.created_at_broker or o.placed_at


def _sort_key(r: FeeRow) -> datetime:
    """Newest-first sort key, tolerant of naive vs tz-aware stored datetimes."""
    at = r.at
    if at is None:
        return _MIN_DT
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at


def _add_row(report: FeeReport, row: FeeRow) -> None:
    report.rows.append(row)
    ct = report.per_customer.setdefault(
        row.customer_id,
        CustomerFeeTotals(customer_id=row.customer_id, agent_id=row.agent_id),
    )
    if row.kind == KIND_SELL:
        ct.num_sells += 1
        ct.sell_fee += row.fee
    else:
        ct.num_virtual += 1
        ct.virtual_fee += row.fee
    ct.total_fee += row.fee
    at = report.per_agent.setdefault(
        row.agent_id, AgentFeeTotals(agent_id=row.agent_id)
    )
    at.num_rows += 1
    at.total_value += row.value
    at.total_fee += row.fee


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
    mark_to_market_days: int = _MARK_TO_MARKET_DAYS,
    max_rows: int = 20000,
) -> FeeReport:
    """Compute the sell-side fee across the filtered orders.

    Loads fully-executed (``state==3``) orders in scope, then per
    ``(customer, isin)``: bills X% of each **bot sell's value**, and — for any
    bot buy lot still unsold after ``mark_to_market_days`` — bills X% of the
    open qty valued at **today's live price** (the sidecar). Rolls up per
    customer (owed) and per agent.
    """
    today = today or datetime.now(timezone.utc).date()

    stmt = (
        select(BrokerOrder)
        .where(BrokerOrder.state == 3)
        # A NULL price would corrupt value/fee math — a fully-executed order
        # should always carry a price; drop any that don't.
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
    if exclude:
        orders = [o for o in orders if not is_excluded(o, exclude)]

    # Resolve each customer's CURRENT agent (broker_orders.agent_id is a
    # fetch-time snapshot that goes stale on reassignment — always bill the
    # current owner).
    cust_agent: dict[UUID, Optional[UUID]] = {}
    cust_ids_present = {o.customer_id for o in orders if o.customer_id is not None}
    if cust_ids_present:
        rows = await db.execute(
            select(Customer.id, Customer.agent_id).where(
                Customer.id.in_(cust_ids_present)
            )
        )
        cust_agent = {cid: aid for cid, aid in rows.all()}

    groups: dict[tuple, list[BrokerOrder]] = {}
    for o in orders:
        groups.setdefault((o.customer_id, o.isin), []).append(o)

    by_tracking = {o.tracking_number: o for o in orders}
    report = FeeReport()
    fee_pct_cache: dict[tuple, Decimal] = {}
    price_cache: dict[str, Optional[int]] = {}

    async def _today_price(isin: str) -> Optional[int]:
        if isin not in price_cache:
            price_cache[isin] = await market_data_client.get_last_price(db, isin)
        return price_cache[isin]

    for (cust_id, isin), group in groups.items():
        agent_of = (
            cust_agent.get(cust_id) if cust_id is not None else group[0].agent_id
        )
        # Fee % resolves per CUSTOMER → agent → global → default (#116), so
        # cache by (customer, agent), not agent alone.
        ck = (cust_id, agent_of)
        if ck not in fee_pct_cache:
            fee_pct_cache[ck] = await get_fee_percent(db, agent_of, customer_id=cust_id)
        fee_pct = fee_pct_cache[ck]
        rate = fee_pct / Decimal("100")
        broker_code = group[0].broker
        group_symbol = next(
            (o.symbol or o.symbol_title for o in group if (o.symbol or o.symbol_title)),
            "",
        ) or ""

        all_buys = [o for o in group if o.order_side == 1 and int(o.executed_volume or 0) > 0]
        all_sells = [o for o in group if o.order_side == 2]

        # (a) Real bot-SELL fees: X% of the sell's value. Manual sells excluded.
        for s in all_sells:
            if not s.is_bot:
                continue
            qty = int(s.executed_volume or 0)
            price = Decimal(s.price) if s.price is not None else Decimal("0")
            if qty <= 0 or price <= 0:
                continue
            value = price * qty
            _add_row(report, FeeRow(
                customer_id=cust_id, agent_id=agent_of, broker=broker_code,
                isin=isin, symbol=s.symbol or group_symbol, kind=KIND_SELL,
                qty=qty, price=price, value=value, fee_percent=fee_pct,
                fee=rate * value, at=_order_ts(s), tracking=s.tracking_number,
            ))

        # (b) 20-day virtual sells on UNSOLD bot buy lots. FIFO all buys vs all
        #     sells ("each buy with the first sell, for all"); keep only the
        #     bot-attributed open lots, mark to market at today's live price.
        if all_buys:
            open_lots = compute_open_lots(
                buys=[_leg(o) for o in all_buys],
                sells=[_leg(o) for o in all_sells],
            )
            for lot in open_lots:
                if lot.qty <= 0:
                    continue
                buy = by_tracking.get(lot.buy_tracking)
                if buy is None or not _is_bot_buy(buy, window_start, window_end):
                    continue
                buy_ts = _order_ts(buy)
                if buy_ts is None:
                    continue
                age = (today - buy_ts.date()).days
                if age <= mark_to_market_days:
                    continue
                price_today = await _today_price(isin)
                if not price_today or price_today <= 0:
                    logger.warning(
                        "fee 20-day: no live price for %s — skipping virtual sell "
                        "(qty=%d, age=%dd)", isin, lot.qty, age,
                    )
                    continue
                price_dec = Decimal(price_today)
                value = price_dec * lot.qty
                _add_row(report, FeeRow(
                    customer_id=cust_id, agent_id=agent_of, broker=broker_code,
                    isin=isin, symbol=group_symbol, kind=KIND_VIRTUAL,
                    qty=lot.qty, price=price_dec, value=value, fee_percent=fee_pct,
                    fee=rate * value, at=buy_ts, tracking=lot.buy_tracking,
                    age_days=age,
                ))

    # Per-customer paid (received-fee ledger) → remaining = owed − paid (#116).
    paid_map = await fee_payments.total_paid_by_customer(
        db, [cid for cid in report.per_customer if cid is not None]
    )
    for cid, ct in report.per_customer.items():
        ct.paid = paid_map.get(cid, Decimal("0")) if cid is not None else Decimal("0")
        ct.remaining = ct.total_fee - ct.paid

    report.rows.sort(key=_sort_key, reverse=True)
    report.grand_value = sum((r.value for r in report.rows), Decimal("0"))
    report.grand_fee = sum((r.fee for r in report.rows), Decimal("0"))
    return report


__all__ = [
    "FeeRow",
    "CustomerFeeTotals",
    "AgentFeeTotals",
    "FeeReport",
    "get_fee_percent",
    "build_fee_report",
    "KIND_SELL",
    "KIND_VIRTUAL",
]
