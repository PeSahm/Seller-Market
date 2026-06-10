"""trade_instructions.auto_sell_only (auto-sell-only instructions)

Revision ID: 0013_ti_auto_sell_only
Revises: 0012_ti_auto_sell_threshold
Create Date: 2026-06-10 00:00:00.000000

Additive: a NOT NULL boolean ``auto_sell_only`` (server_default false) on
``trade_instructions``. True = watch-only — the row keeps ``side = 1`` so the
bot's auto-sell monitor arms it, but locust + cache warmup SKIP the rendered
section so nothing fires at market open. Lets the operator arm auto-sell for
an EXISTING holding without placing a buy.

Revision id ≤32 chars (the ``alembic_version.version_num`` cap).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_ti_auto_sell_only"
down_revision: Union[str, Sequence[str], None] = "0012_ti_auto_sell_threshold"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trade_instructions",
        sa.Column(
            "auto_sell_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("trade_instructions", "auto_sell_only")
