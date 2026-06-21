"""instrument_close_prices: manual global per-symbol close price for the fee report

Revision ID: 0018_instrument_close_price
Revises: 0017_mgmt_instances
Create Date: 2026-06-21 00:00:00.000000

Replaces the automatic 20-day mark-to-market with a manual, price-driven close.
A row per ISIN (PK) holds a GLOBAL close price; the fee report realizes any open
bot-buy remainder of that instrument at it (editable anytime, fee recomputes
live; deleting the row re-opens the position). Additive — a new standalone table,
no other schema change. Also drops any stale ``mark_to_market_days`` setting row
(the 20-day rule is retired; its value is no longer read).

Revision id ≤32 chars (the ``alembic_version.version_num`` cap).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_instrument_close_price"
down_revision: str | Sequence[str] | None = "0017_mgmt_instances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instrument_close_prices",
        sa.Column("isin", sa.String(length=64), primary_key=True),
        sa.Column("close_price", sa.Numeric(20, 4), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # The 20-day rule is retired; drop any stale saved value so it can't linger.
    op.execute("DELETE FROM settings WHERE key = 'mark_to_market_days'")


def downgrade() -> None:
    op.drop_table("instrument_close_prices")
