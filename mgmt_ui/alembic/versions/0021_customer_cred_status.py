"""customers credential health: status / checked_at / message

Revision ID: 0021_customer_cred_status
Revises: 0020_service_probes
Create Date: 2026-06-23 00:00:00.000000

Adds per-customer credential health, set by the mandatory verify-on-save flow
and the daily noon credential checker. Additive and backward compatible:
existing rows backfill to ``unknown`` (NOT ``invalid``) so nothing false-alarms
before the first check. A partial index supports the dashboard "needs attention"
query (``WHERE credential_status = 'invalid'``).

Revision id <=32 chars (the ``alembic_version.version_num`` cap).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_customer_cred_status"
down_revision: str | Sequence[str] | None = "0020_service_probes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Native Postgres ENUM. Mirrors the SAEnum on ``app.models.customers``.
customer_credential_status = postgresql.ENUM(
    "unknown",
    "valid",
    "invalid",
    "transient",
    name="customer_credential_status",
)


def upgrade() -> None:
    # Create the enum type first; reference it with create_type=False on the
    # column so the ADD COLUMN doesn't try to (re)create it.
    customer_credential_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "customers",
        sa.Column(
            "credential_status",
            postgresql.ENUM(
                "unknown", "valid", "invalid", "transient",
                name="customer_credential_status",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )
    op.add_column(
        "customers",
        sa.Column("credential_checked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "customers",
        sa.Column("credential_message", sa.String(length=512), nullable=True),
    )
    op.create_index(
        "ix_customers_cred_invalid",
        "customers",
        ["credential_status"],
        postgresql_where=sa.text("credential_status = 'invalid'"),
    )


def downgrade() -> None:
    op.drop_index("ix_customers_cred_invalid", table_name="customers")
    op.drop_column("customers", "credential_message")
    op.drop_column("customers", "credential_checked_at")
    op.drop_column("customers", "credential_status")
    customer_credential_status.drop(op.get_bind(), checkfirst=True)
