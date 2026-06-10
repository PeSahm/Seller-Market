"""Assemble the Active-auto-sell page rows (#110).

Joins the armed trade-instructions (per-instruction ``auto_sell_threshold``) with
the LIVE best-buy queue (from the market-data sidecar) and a "fired today" flag
(side=2 bot fires from ``order_fires``). Degrades gracefully — a sidecar hiccup
just shows ``buy_volume = None`` (the page never 500s).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order_fires import OrderFire
from app.services import market_data_client
from app.services import trade_instructions as services_ti


async def build_auto_sell_rows(
    db: AsyncSession,
    agent_id: Optional[UUID] = None,
) -> list[dict]:
    """Return one row per armed (customer, isin), newest-armed first.

    Each row: ``customer_id, customer, agent_id, broker, isin, threshold,
    buy_volume (live or None), triggered (buy_volume<=threshold), fired_today,
    sell_only (auto-sell-only watch row — no buy fires at open)``.
    """
    armed = await services_ti.list_armed_auto_sell(db, agent_id)
    if not armed:
        return []

    # "Fired today" = a side=2 bot fire today for that (customer, isin). run_date
    # is the fire's UTC date, so compare against UTC today.
    today = datetime.now(timezone.utc).date()
    customer_ids = {c.id for _ti, c in armed}
    fired_rows = (
        await db.execute(
            select(OrderFire.customer_id, OrderFire.isin)
            .where(OrderFire.side == 2)
            .where(OrderFire.run_date == today)
            .where(OrderFire.customer_id.in_(customer_ids))
        )
    ).all()
    fired = {(r[0], r[1]) for r in fired_rows}

    # One live-queue lookup per UNIQUE isin (deduped).
    queue_by_isin: dict[str, Optional[int]] = {}
    for isin in {ti.isin for ti, _c in armed}:
        q = await market_data_client.get_queue(db, isin)
        queue_by_isin[isin] = (q or {}).get("buy_volume") if q else None

    rows: list[dict] = []
    for ti, c in armed:
        bv = queue_by_isin.get(ti.isin)
        rows.append({
            "customer_id": c.id,
            "customer": c.display_name,
            "agent_id": c.agent_id,
            "broker": c.broker,
            "isin": ti.isin,
            "threshold": ti.auto_sell_threshold,
            "buy_volume": bv,
            "triggered": bv is not None and bv <= ti.auto_sell_threshold,
            "fired_today": (c.id, ti.isin) in fired,
            # getattr keeps older test fakes (built before the column) working.
            "sell_only": bool(getattr(ti, "auto_sell_only", False)),
        })
    return rows


__all__ = ["build_auto_sell_rows"]
