"""brokers: per-broker base_domain (OnlinePlus tenant host)

Revision ID: 0022_broker_base_domain
Revises: 0021_customer_cred_status
Create Date: 2026-06-26 00:00:00.000000

OnlinePlus (Tadbir Online+) tenants do NOT follow one host convention: Hafez is
``hafezbroker.ir`` but e.g. Danaye Novin is ``dnovinbr.ir`` (not
``dnovinbroker.ir``). So a per-broker base domain is needed. ``base_domain`` is
the bare tenant domain (e.g. ``dnovinbr.ir``); the adapter builds
``online.{domain}`` (web) + ``api.{domain}`` (api, auto-discovered from the web
login page). Nullable + ignored by ephoenix/exir; when null the OnlinePlus
adapter falls back to the ``online.{code}broker.ir`` convention (so an existing
``hafez`` broker keeps working without it). Additive, backward compatible.

Revision id <=32 chars (the ``alembic_version.version_num`` cap).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_broker_base_domain"
down_revision: str | Sequence[str] | None = "0021_customer_cred_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "brokers",
        sa.Column("base_domain", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("brokers", "base_domain")
