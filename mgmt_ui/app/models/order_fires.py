from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class OrderFire(Base):
    """One bot order-fire record — "the bot fired an order for this
    customer/broker/isin/side on this run".

    Written by the bot (``locustfile_new.py::_emit_order_fire``, one line per
    account per run) into ``run_results/order_fires_<YYYYMMDD>.jsonl`` and
    pulled in by :mod:`app.services.fire_log_ingestor`. It exists to:

    * **authoritatively tag bot buys** — reconciliation sets
      ``broker_orders.is_bot=true`` for the matching (customer, isin, side,
      date), replacing the market-open time-window heuristic on days the
      fire-log covers (the accounts also see manual trades);
    * give a **daily "which customers ran" roster** (group by ``run_date``).

    Idempotency is on the bot-generated ``fire_uid`` (UNIQUE) — the ingestor
    re-reads the growing daily JSONL each tick and ``ON CONFLICT DO NOTHING``
    skips already-seen lines, so no cursor or delete-on-consume is needed.
    """

    __tablename__ = "order_fires"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # Resolved from (agent, broker, account_username); nullable when the
    # account isn't a Customer row (recorded, not dropped).
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    broker: Mapped[str] = mapped_column(String(255), nullable=False)
    account_username: Mapped[str] = mapped_column(String(255), nullable=False)
    isin: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[int] = mapped_column(Integer, nullable=False)
    fired_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    # Local calendar date of the fire — the daily-roster grouping key.
    run_date: Mapped[date] = mapped_column(sa.Date, nullable=False, index=True)
    # Bot-generated UUID hex; the idempotency key for re-reads of the append log.
    fire_uid: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # Broker serial number from the bot's NewOrder response — the durable key
    # the reconciler matches against ``broker_orders.serial_number``.
    serial_number: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    tracking_number: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    reconciled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    raw_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    __table_args__ = (
        sa.Index("ix_order_fires_customer_isin_side", "customer_id", "isin", "side"),
        sa.Index("ix_order_fires_serial_number", "serial_number"),
    )
