from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class BrokerOrder(Base):
    """One executed/known order pulled DIRECTLY from the broker's ``GetOrders``.

    This is the mgmt UI's OWN source of truth for trade history — distinct
    from :class:`app.models.trades.TradeResult`, which is fed by the bot's
    ``order_results/*.json`` SFTP pipeline and only ever sees OPEN orders
    (``GetOpenOrders``), silently dropping anything already fully executed.

    Unlike ``TradeResult`` this table:

    * has **no run_id** — GetOrders rows aren't tied to a mgmt-UI run;
    * stores **sells** as well as buys (no ``TradeInstruction`` gate), so the
      profit/fee matcher can pair a bot buy with the later sell;
    * carries the broker fee + value columns (``total_fee``,
      ``executed_amount``, ``net_traded_value``, ``pam_code``) that the fee
      report needs.

    Idempotency: ``tracking_number`` is UNIQUE and the reconciler upserts with
    ``ON CONFLICT (tracking_number) DO UPDATE`` — GetOrders is a poll of
    mutable broker state (``executed_volume`` / ``state`` / fees change as an
    order fills), so we refresh the mutable fields on each fetch but never
    rewrite the immutable ``created_at_broker`` / ``first_seen_at``.
    """

    __tablename__ = "broker_orders"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # Nullable: an order whose account isn't (yet) a Customer row is still
    # recorded (and surfaced as "unassigned") rather than dropped on the
    # floor — the exact silent-drop bug the trade_ingestor has.
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # Denormalized from the customer for fast per-agent filtering/rollups in
    # the report without a join. Nullable when customer_id is null.
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    broker: Mapped[str] = mapped_column(String(255), nullable=False)
    account_username: Mapped[str] = mapped_column(String(255), nullable=False)
    pam_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tracking_number: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True
    )
    # The GetOrders row's own ``id`` field — kept for cross-referencing the
    # broker UI; NOT the dedup key (tracking_number is).
    broker_order_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    isin: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    symbol_title: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # 1 = buy, 2 = sell (matches TradeInstruction.side + GetOrders.orderSide).
    order_side: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    executed_volume: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    total_fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 4), nullable=True)
    executed_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(24, 4), nullable=True
    )
    net_traded_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(24, 4), nullable=True
    )
    state: Mapped[int] = mapped_column(Integer, nullable=False)
    state_desc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_done: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # ``date`` from the GetOrders row — when the order was placed. The
    # market-open window (08:44:59–08:45:03) on THIS field is the historical
    # bot-attribution heuristic.
    placed_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    # ``created`` from the GetOrders row (sub-second placement time).
    created_at_broker: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    execution_date: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    # True once this BUY is confirmed to have been fired by the bot — set by
    # the fire-log reconciler (authoritative, via tracking_number) or the
    # market-open time-window heuristic for historical rows.
    is_bot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    raw_json: Mapped[Any] = mapped_column(JSONB, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    fetched_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    __table_args__ = (
        sa.Index(
            "ix_broker_orders_customer_isin_side_state",
            "customer_id",
            "isin",
            "order_side",
            "state",
        ),
        sa.Index("ix_broker_orders_placed_at", "placed_at"),
        sa.Index("ix_broker_orders_agent_id_placed_at", "agent_id", "placed_at"),
    )
