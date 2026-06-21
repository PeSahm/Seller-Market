"""instrument_close_prices: re-key per (customer_id, isin) so a close is per-customer

Revision ID: 0019_cp_per_customer
Revises: 0018_instrument_close_price
Create Date: 2026-06-21 00:00:00.000000

The manual close price moves from one GLOBAL price per symbol to one price per
(customer, symbol), so the operator can close one customer's holding of a symbol
and leave another customer's open. The 0018 table was deployed empty (no close
prices saved yet), so this drops + recreates it keyed by ``(customer_id, isin)``
— no data to migrate.

Revision id ≤32 chars (the ``alembic_version.version_num`` cap).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_cp_per_customer"
down_revision: str | Sequence[str] | None = "0018_instrument_close_price"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The 0018 table holds no rows yet — drop + recreate with the composite key.
    op.drop_table("instrument_close_prices")
    op.create_table(
        "instrument_close_prices",
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
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
        sa.CheckConstraint(
            "close_price > 0",
            name="ck_instrument_close_prices_close_price_positive",
        ),
    )


def downgrade() -> None:
    op.drop_table("instrument_close_prices")
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
        sa.CheckConstraint(
            "close_price > 0",
            name="ck_instrument_close_prices_close_price_positive",
        ),
    )
