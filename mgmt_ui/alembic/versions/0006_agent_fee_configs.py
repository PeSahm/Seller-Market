"""agent_fee_configs — per-agent profit-share fee override

Revision ID: 0006_agent_fee_configs
Revises: 0005_broker_orders
Create Date: 2026-06-01 18:30:00.000000

The operator charges a profit-share fee (X% of the profit the bot generates).
The global default lives in the ``settings`` table (``profit_fee_percent``);
this table holds per-agent overrides for agents on a different rate. Absent a
row, the report falls back to the global setting.

Purely additive — downgrade drops the table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
# "0006_agent_fee_configs" is 22 chars — within the VARCHAR(32) cap.
revision: str = "0006_agent_fee_configs"
down_revision: Union[str, Sequence[str], None] = "0005_broker_orders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_fee_configs",
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("fee_percent", sa.Numeric(6, 4), nullable=False),
        sa.Column("effective_from", sa.Date, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
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


def downgrade() -> None:
    op.drop_table("agent_fee_configs")
