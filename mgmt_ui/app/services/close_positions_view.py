"""Open-position aggregation for the Close-positions page.

Reuses :func:`app.services.profit_report.build_fee_report` (same scope/window/
exclude as the fees page) so the set of OPEN positions shown here is identical
to what the fee report treats as open — no divergent query. Each row aggregates
the unsold bot-buy remainder per ISIN with its weighted average buy price, the
latest day price (the prefill default for closing), and the current saved global
close price (if any).
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
    isin: str
    symbol: str
    symbol_title: Optional[str]
    open_qty: int
    avg_buy_price: Decimal
    latest_price: Optional[int]   # sidecar last price — the prefill default
    saved_price: Optional[Decimal]  # the saved global close price, if set
    customer_count: int


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
    """Aggregate the OPEN bot-buy remainder per ISIN, in scope.

    ``agent_id=None`` → every open position (admin); set → only that agent's
    own customers' open symbols (the route passes the agent's id). Uses the same
    settings-backed defaults (robot_start_date / bot window / exclusions) the
    fee pages use, so the open set matches exactly.
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

    agg: dict[str, dict] = {}
    for r in report.buy_rows:
        if r.open_volume <= 0:
            continue
        o = r.buy
        a = agg.get(o.isin)
        if a is None:
            a = {
                "open_qty": 0,
                "weighted": Decimal("0"),
                "symbol": "",
                "symbol_title": None,
                "custs": set(),
            }
            agg[o.isin] = a
        a["open_qty"] += r.open_volume
        a["weighted"] += (
            Decimal(o.price) if o.price is not None else Decimal("0")
        ) * r.open_volume
        if not a["symbol"] and (o.symbol or o.symbol_title):
            a["symbol"] = o.symbol or ""
            a["symbol_title"] = o.symbol_title
        if o.customer_id is not None:
            a["custs"].add(o.customer_id)

    saved_map = await close_prices_svc.get_close_prices(db, list(agg.keys()))
    rows: list[OpenPositionRow] = []
    for isin, a in agg.items():
        open_qty = a["open_qty"]
        avg = (a["weighted"] / open_qty) if open_qty else Decimal("0")
        latest = await market_data_client.get_last_price(db, isin)
        rows.append(
            OpenPositionRow(
                isin=isin,
                symbol=a["symbol"],
                symbol_title=a["symbol_title"],
                open_qty=open_qty,
                avg_buy_price=avg,
                latest_price=latest,
                saved_price=saved_map.get(isin),
                customer_count=len(a["custs"]),
            )
        )
    rows.sort(key=lambda p: (p.symbol or p.isin))
    return rows


async def list_open_isins(
    db: AsyncSession,
    *,
    agent_id: Optional[UUID] = None,
    broker: Optional[str] = None,
) -> set[str]:
    """The set of ISINs with an OPEN bot-buy remainder in scope.

    A lean variant of :func:`build_open_positions` for the agent authorization
    guard — it runs the same report but SKIPS the per-ISIN market-price fetch, so
    a write (price/clear) is never blocked on the sidecar being reachable.
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
    return {
        r.buy.isin
        for r in report.buy_rows
        if r.open_volume > 0 and r.buy.isin
    }


__all__ = ["OpenPositionRow", "build_open_positions", "list_open_isins"]
