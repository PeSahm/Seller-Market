from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class InstrumentClosePrice(Base):
    """A manual PER-CUSTOMER close price for the fee report.

    Replaces the automatic 20-day mark-to-market. When a (customer, ISIN) has a
    row here, that customer's OPEN bot-buy remainder of the instrument is
    realized into the fee at ``close_price`` — profit → X% of the gain, loss →
    the fixed per-agent loss fee, break-even → no fee. Closing one customer's
    holding of a symbol does NOT touch another customer's. The price defaults to
    the latest market price but is editable anytime (e.g. after a مجمع dividend
    drop); the fee recomputes live; deleting the row re-opens the position.

    Composite PK ``(customer_id, isin)`` → one close price per customer-position.
    ``close_price`` is a Rial value (``Numeric(20,4)``, matching ``broker_orders.price``).
    """

    __tablename__ = "instrument_close_prices"
    __table_args__ = (
        sa.CheckConstraint(
            "close_price > 0",
            name="ck_instrument_close_prices_close_price_positive",
        ),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    isin: Mapped[str] = mapped_column(String(64), primary_key=True)
    close_price: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    # Free-text note for why this price was set (e.g. "post-مجمع").
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )
