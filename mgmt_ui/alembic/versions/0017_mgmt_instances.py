"""mgmt_instances: per-instance heartbeat for the HA page (#156)

Revision ID: 0017_mgmt_instances
Revises: 0016_bo_fee_decline
Create Date: 2026-06-21 00:00:00.000000

Each running mgmt instance upserts its own row (name PK) every ~15s to the
SHARED database, so any instance's ``/admin/ha`` page can list all instances
with which holds worker leadership and which DB each is serving. Additive — a
new standalone table, no FK, safe both deploy directions.

Revision id ≤32 chars (the ``alembic_version.version_num`` cap).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_mgmt_instances"
down_revision: str | Sequence[str] | None = "0016_bo_fee_decline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mgmt_instances",
        sa.Column("name", sa.String(length=128), primary_key=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column(
            "active_db", sa.String(length=16), nullable=False, server_default=sa.text("'main'")
        ),
        sa.Column(
            "is_leader", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("mgmt_instances")
