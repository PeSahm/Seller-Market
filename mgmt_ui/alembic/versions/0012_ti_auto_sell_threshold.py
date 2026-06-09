"""trade_instructions.auto_sell_threshold (#110 auto-sell)

Revision ID: 0012_ti_auto_sell_threshold
Revises: 0011_agent_loss_fee
Create Date: 2026-06-09 00:00:00.000000

Additive: a NULLABLE integer ``auto_sell_threshold`` (a best-buy-queue SHARE
COUNT) on ``trade_instructions``. NULL = no auto-sell for that instruction. When
set on a BUY instruction, the bot watches that instrument's buy-queue over the
RLC WebSocket and sells the held position (chunked, at the floor) once the queue
drops to/below this count.

Revision id ≤32 chars (the ``alembic_version.version_num`` cap).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_ti_auto_sell_threshold"
down_revision: Union[str, Sequence[str], None] = "0011_agent_loss_fee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trade_instructions",
        sa.Column("auto_sell_threshold", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trade_instructions", "auto_sell_threshold")
