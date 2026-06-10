"""auto_sell_reload_status — bot-applied auto-sell thresholds (#110 hot-reload)

Revision ID: 0014_as_reload_status
Revises: 0013_ti_auto_sell_only
Create Date: 2026-06-10 00:00:00.000000

The bot's auto-sell monitor hot-reloads ``config.ini`` and writes a
``run_results/auto_sell_status.json`` marker listing WHICH thresholds it has
actually applied. The fire-log ingestor SFTP-reads that marker and replaces this
table's rows for the stack, so the Active-auto-sell page can confirm to the
operator that a live threshold edit landed on the bot (vs still pending reload).

Keyed (stack_id, account, isin) — NOT (stack_id, isin): two customers (distinct
broker accounts) on the SAME stack can arm the SAME instrument, and the marker
carries one entry per (account, isin) target.

Revision id ≤32 chars (the ``alembic_version.version_num`` cap).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_as_reload_status"
down_revision: Union[str, Sequence[str], None] = "0013_ti_auto_sell_only"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auto_sell_reload_status",
        sa.Column("stack_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account", sa.String(length=128), nullable=False),
        sa.Column("isin", sa.String(length=64), nullable=False),
        sa.Column("applied_threshold", sa.Integer(), nullable=False),
        sa.Column("applied_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "stack_id", "account", "isin", name="pk_auto_sell_reload_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("auto_sell_reload_status")
