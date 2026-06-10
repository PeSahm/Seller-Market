from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class TradeInstruction(Base):
    """One (isin, side) pair a Customer should trade.

    Many ``TradeInstruction`` rows can hang off a single ``Customer``;
    the renderer crosses Customer × enabled TradeInstruction to produce
    one ``[section]`` per trade — keeping the bot-facing config.ini
    unchanged in shape after the 0003 split.

    The UNIQUE on ``(customer_id, isin, side)`` prevents the operator
    from adding the same trade twice to one customer; the service layer
    translates the IntegrityError into a friendly "already exists"
    message. ``section_name`` carries its own global UNIQUE so the
    rendered INI section header is guaranteed unique across the fleet.
    """

    __tablename__ = "trade_instructions"
    __table_args__ = (
        CheckConstraint("side IN (1, 2)", name="ck_trade_instructions_side"),
        sa.UniqueConstraint(
            "customer_id",
            "isin",
            "side",
            name="uq_trade_instructions_customer_isin_side",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    isin: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[int] = mapped_column(Integer, nullable=False)
    # #110 auto-sell: best-buy-queue SHARE COUNT below which the bot sells the
    # held position (chunked, at the floor). NULL = no auto-sell. Set on BUY
    # instructions via the form; the bot reads it from config.ini.
    auto_sell_threshold: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Auto-sell-only: watch an EXISTING holding without buying. The row keeps
    # side = 1 so the bot's monitor arms it, but locust + cache warmup skip the
    # rendered section — nothing fires at market open.
    auto_sell_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa.false(), default=False
    )
    section_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
