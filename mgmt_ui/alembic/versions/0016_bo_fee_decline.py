"""broker_orders: per-order fee decline (exclude a manual trade from the fee)

Revision ID: 0016_bo_fee_decline
Revises: 0015_bo_dedup_acct_date
Create Date: 2026-06-13 00:00:00.000000

Additive, both NULLABLE: ``fee_excluded_at`` (TIMESTAMPTZ) + ``fee_excluded_by``
(UUID FK users). Presence of ``fee_excluded_at`` = this executed order was
declined ("the customer's own manual trade, not the bot's") and is dropped from
the fee report's grouping/matching; NULL = active/billed. ``fee_excluded_by`` is
the actor (agent or admin) for the audit trail; ON DELETE SET NULL so removing a
user never deletes order history.

Both columns are deliberately kept OUT of the broker_orders ON CONFLICT update
set (``_MUTABLE_ON_CONFLICT``), so a later GetOrders re-fetch of the same order
never clears a decline and silently re-bills it.

A partial index on the declined complement keeps the Undo-panel query cheap; the
report itself filters on ``fee_excluded_at IS NULL`` on every build.

Revision id ≤32 chars (the ``alembic_version.version_num`` cap).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0016_bo_fee_decline"
down_revision: Union[str, Sequence[str], None] = "0015_bo_dedup_acct_date"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broker_orders",
        sa.Column("fee_excluded_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "broker_orders",
        sa.Column(
            "fee_excluded_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_broker_orders_fee_excluded_at",
        "broker_orders",
        ["fee_excluded_at"],
        postgresql_where=sa.text("fee_excluded_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_broker_orders_fee_excluded_at", table_name="broker_orders")
    op.drop_column("broker_orders", "fee_excluded_by")
    op.drop_column("broker_orders", "fee_excluded_at")
