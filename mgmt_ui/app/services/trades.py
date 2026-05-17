"""Read service for trade_results — used by /admin/trades and /agent/trades.

Pairs with :mod:`app.services.trade_ingestor` (the write side). The router
layer hands us a few common filters off the query string and we project
:class:`app.models.trades.TradeResult` rows back out. Tenant scoping
(``agent_id``) is enforced by joining through
:class:`app.models.customers.Customer` so a malicious agent can never see
another agent's trades just by passing a different ``customer_id``.

The detail view is intentionally split into ``get_trade`` (returns the
row) + a separate ``Customer`` fetch by the caller — :func:`can_user_see_trade`
takes both so this service stays free of joins. The route layer pays the
extra one-row lookup, which is cheap.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customers import Customer
from app.models.trades import TradeResult
from app.models.users import User


async def list_trades(
    db: AsyncSession,
    *,
    agent_id: Optional[UUID] = None,
    customer_id: Optional[UUID] = None,
    broker: Optional[str] = None,
    symbol_or_isin: Optional[str] = None,
    state: Optional[int] = None,
    side: Optional[int] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 200,
) -> list[TradeResult]:
    """Filter trades on common dimensions; newest-first.

    ``agent_id`` is the tenant scope: when set, we join through
    :class:`Customer` so only that agent's trades are visible. The
    ``broker`` filter also needs the :class:`Customer` join because
    broker lives on the customer record, not on the trade row itself.

    We join :class:`Customer` at most once even if both ``agent_id`` and
    ``broker`` are set — SQLAlchemy's ``select.join`` against the same
    target twice raises ``InvalidRequestError`` otherwise. The dedupe
    check uses ``stmt.get_final_froms()`` indirectly by tracking a local
    flag.

    ``limit`` defaults to 200 to match the UI's table page; callers
    paginating manually can override.
    """
    stmt = select(TradeResult).order_by(desc(TradeResult.ingested_at)).limit(limit)
    needs_customer_join = agent_id is not None or broker is not None
    if needs_customer_join:
        stmt = stmt.join(Customer, Customer.id == TradeResult.customer_id)
        if agent_id is not None:
            stmt = stmt.where(Customer.agent_id == agent_id)
        if broker:
            stmt = stmt.where(Customer.broker == broker)

    if customer_id is not None:
        stmt = stmt.where(TradeResult.customer_id == customer_id)
    if symbol_or_isin:
        # The UI uses a single search box for either field; OR them so
        # the user doesn't have to pick.
        stmt = stmt.where(
            (TradeResult.symbol == symbol_or_isin)
            | (TradeResult.isin == symbol_or_isin)
        )
    if state is not None:
        stmt = stmt.where(TradeResult.state == state)
    if side is not None:
        stmt = stmt.where(TradeResult.side == side)
    if since is not None:
        stmt = stmt.where(TradeResult.ingested_at >= since)
    if until is not None:
        stmt = stmt.where(TradeResult.ingested_at <= until)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_trade(db: AsyncSession, trade_id: UUID) -> Optional[TradeResult]:
    """Fetch one trade by PK. ``None`` if missing.

    The router is expected to load the matching :class:`Customer` row in
    a second call and feed both to :func:`can_user_see_trade` to gate
    the response. Keeping the join out of this service keeps it small
    and reusable for the JSON-export path that doesn't need the customer.
    """
    return await db.get(TradeResult, trade_id)


def can_user_see_trade(user: User, trade: TradeResult, customer: Customer) -> bool:
    """Permission check: admins see all trades, agents see only their own.

    Pure function — no DB, no I/O. We compare on ``customer.agent_id``
    (the stack owner) rather than anything on the trade row, because
    :class:`TradeResult` doesn't carry the agent identity directly; it
    inherits it via ``customer_id``. Callers MUST pass the customer
    that matches the trade — there's no defensive cross-check here,
    that's the route layer's job.
    """
    return user.role == "admin" or customer.agent_id == user.id


__all__ = [
    "list_trades",
    "get_trade",
    "can_user_see_trade",
]
