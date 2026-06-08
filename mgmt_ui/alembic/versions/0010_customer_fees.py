"""customer fee % override + received-payments ledger (#116)

Revision ID: 0010_customer_fees
Revises: 0009_bo_tracking_composite
Create Date: 2026-06-08 00:00:00.000000

Two additions, both purely additive:

* ``customers.fee_percent`` — a NULLABLE per-customer override of the
  profit-share fee %. Resolution order becomes customer → agent → global →
  default. NULL means "no override; fall back to the agent/global rate".
* ``customer_fee_payments`` — a ledger of fees the operator has actually
  RECEIVED for a customer (e.g. "Azadi paid 1,000,000 rial"). The fee report
  shows, per customer, owed (computed) − paid (Σ this ledger) = remaining.

Keep the revision id ≤32 chars (the ``alembic_version.version_num`` cap that
bit us at 0009).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_customer_fees"
down_revision: Union[str, Sequence[str], None] = "0009_bo_tracking_composite"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column("fee_percent", sa.Numeric(6, 4), nullable=True),
    )
    op.create_table(
        "customer_fee_payments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("amount", sa.Numeric(24, 4), nullable=False),
        sa.Column("paid_at", sa.Date, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "recorded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("customer_fee_payments")
    op.drop_column("customers", "fee_percent")
