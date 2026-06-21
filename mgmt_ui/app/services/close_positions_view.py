"""Open-position aggregation for the Close-positions page.

Reuses :func:`app.services.profit_report.build_fee_report` (same scope/window/
exclude as the fees page) so the set of OPEN positions shown here is identical to
what the fee report treats as open — no divergent query. One row PER (customer,
ISIN) with the unsold bot-buy remainder, its weighted average buy price, the
latest day price (the prefill default for closing), and the current saved close
price (if any). The customer name is resolved by the route/template.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import close_prices as close_prices_svc
from app.services import market_data_client, settings_store
from app.services.broker_orders import parse_exclusions
from app.services.profit_report import build_fee_report


@dataclass
class OpenPositionRow:
    customer_id: UUID
    isin: str
    symbol: str
    symbol_title: Optional[str]
    open_qty: int
    avg_buy_price: Decimal
    latest_price: Optional[int]     # sidecar last price — the prefill default
    saved_price: Optional[Decimal]  # the saved close price for this position


def _parse_date(s: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat((s or "").strip())
    except (ValueError, TypeError):
        return None


def _parse_time(s: Optional[str]) -> Optional[time]:
    try:
        return time.fromisoformat((s or "").strip())
    except (ValueError, TypeError):
        return None


async def build_open_positions(
    db: AsyncSession,
    *,
    agent_id: Optional[UUID] = None,
    broker: Optional[str] = None,
) -> list[OpenPositionRow]:
    """One OPEN position per (customer, ISIN), in scope.

    ``agent_id=None`` → every open position (admin); set → only that agent's own
    customers' positions. Uses the same settings-backed defaults (robot_start_date
    / bot window / exclusions) the fee pages use, so the open set matches exactly.
    """
    since = _parse_date(await settings_store.get_setting(db, "robot_start_date"))
    ws = _parse_time(await settings_store.get_setting(db, "bot_window_start"))
    we = _parse_time(await settings_store.get_setting(db, "bot_window_end"))
    exclude = parse_exclusions(
        (await settings_store.get_setting(db, "excluded_instruments")) or ""
    )

    report = await build_fee_report(
        db,
        agent_id=agent_id,
        broker=broker or None,
        since=since,
        window_start=ws,
        window_end=we,
        exclude=exclude,
    )

    agg: dict[tuple, dict] = {}
    for r in report.buy_rows:
        if r.open_volume <= 0:
            continue
        o = r.buy
        if o.customer_id is None or not o.isin:
            continue  # unattributed orders can't be per-customer closed
        key = (o.customer_id, o.isin)
        a = agg.get(key)
        if a is None:
            a = {"open_qty": 0, "weighted": Decimal("0"), "symbol": "", "symbol_title": None}
            agg[key] = a
        a["open_qty"] += r.open_volume
        a["weighted"] += (
            Decimal(o.price) if o.price is not None else Decimal("0")
        ) * r.open_volume
        if not a["symbol"] and (o.symbol or o.symbol_title):
            a["symbol"] = o.symbol or ""
            a["symbol_title"] = o.symbol_title

    saved_map = await close_prices_svc.get_close_prices(db, list(agg.keys()))
    price_cache: dict[str, Optional[int]] = {}  # one sidecar call per distinct ISIN
    rows: list[OpenPositionRow] = []
    for (cust_id, isin), a in agg.items():
        open_qty = a["open_qty"]
        avg = (a["weighted"] / open_qty) if open_qty else Decimal("0")
        if isin not in price_cache:
            price_cache[isin] = await market_data_client.get_last_price(db, isin)
        rows.append(
            OpenPositionRow(
                customer_id=cust_id,
                isin=isin,
                symbol=a["symbol"],
                symbol_title=a["symbol_title"],
                open_qty=open_qty,
                avg_buy_price=avg,
                latest_price=price_cache[isin],
                saved_price=saved_map.get((cust_id, isin)),
            )
        )
    rows.sort(key=lambda p: (p.symbol or p.isin))
    return rows


__all__ = ["OpenPositionRow", "build_open_positions"]
