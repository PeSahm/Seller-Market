"""service_probe_results: per-server service reachability matrix

Revision ID: 0020_service_probes
Revises: 0019_cp_per_customer
Create Date: 2026-06-22 00:00:00.000000

A background worker SSH-probes every managed server for every service it depends
on and stores the latest result here (one row per ``(server_id, target_key)``),
rendered as the endpoint x server matrix on ``/admin/server-services``. The
manual "Deep check" tier writes authenticated rows here too. Additive — a new
standalone table, no other schema change.

Revision id <=32 chars (the ``alembic_version.version_num`` cap).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_service_probes"
down_revision: str | Sequence[str] | None = "0019_cp_per_customer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_probe_results",
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("servers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("target_key", sa.String(length=128), primary_key=True),
        sa.Column("group_name", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "probed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("service_probe_results")
