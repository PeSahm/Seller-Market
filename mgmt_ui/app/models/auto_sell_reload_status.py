from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TIMESTAMP

from app.db_base import Base


class AutoSellReloadStatus(Base):
    """The auto-sell threshold a live bot has ACTUALLY applied, per position.

    The bot's hot-reload supervisor (``SellerMarket/auto_sell_monitor.py``)
    overwrites ``run_results/auto_sell_status.json`` whenever it applies a config
    change; :mod:`app.services.fire_log_ingestor` SFTP-reads it per tick and
    REPLACES this stack's rows with the marker's armed set (the marker is
    authoritative + overwritten; no history is kept). The Active-auto-sell page
    then compares ``applied_threshold`` against the live DB threshold to show
    the operator "✓ applied HH:MM" vs "⏳ pending bot reload" — closing the
    trust loop for real-time threshold editing (#110).

    Keyed (stack_id, account, isin) — NOT (stack_id, isin): two customers
    (distinct broker accounts) on the SAME stack can arm the SAME instrument,
    and the bot marker carries one entry per (account, isin) target. ``account``
    is the broker username (== ``customers.username``), which is how the view
    joins a row back to its customer.
    """

    __tablename__ = "auto_sell_reload_status"

    stack_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    account: Mapped[str] = mapped_column(String(128), primary_key=True)
    isin: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
