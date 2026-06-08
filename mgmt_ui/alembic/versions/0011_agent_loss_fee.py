"""per-agent fixed loss-fee override for the 20-day mark-to-market (#111 follow-up)

Revision ID: 0011_agent_loss_fee
Revises: 0010_customer_fees
Create Date: 2026-06-08 00:00:00.000000

When a bot-bought position is still unsold after 20 days and is in LOSS at
today's market price, the operator bills a FIXED fee (instead of a % of a
non-existent gain). The amount is configurable per agent — this adds the
nullable override column; the global default lives in the ``settings`` table
(``mark_to_market_loss_fee_toman``). Stored in **Toman** (the operator's unit);
the report converts ×10 to Rial. Purely additive.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_agent_loss_fee"
down_revision: Union[str, Sequence[str], None] = "0010_customer_fees"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_fee_configs",
        sa.Column("loss_fee_toman", sa.Numeric(18, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_fee_configs", "loss_fee_toman")
