"""brokers: seed the Mofid / Orbis broker row

Revision ID: 0023_seed_mofid
Revises: 0022_broker_base_domain
Create Date: 2026-06-30 00:00:00.000000

Adds the single Mofid / Orbis (easytrader.ir) broker row — the 4th family
(``family='mofid'``). Mofid is ONE broker (no per-tenant subdomain), its hosts
are hardcoded in the adapter, so no new column is needed. Idempotent: skips if a
``mofid`` row already exists (e.g. added via the broker-admin CRUD). The adapter
ships in the same PR, so ``family_of('mofid')`` always has an adapter behind it.

Revision id <=32 chars (the ``alembic_version.version_num`` cap).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_seed_mofid"
down_revision: str | Sequence[str] | None = "0022_broker_base_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(
        sa.text("SELECT 1 FROM brokers WHERE code = :c"), {"c": "mofid"}
    ).first()
    if exists:
        return
    brokers_tbl = sa.table(
        "brokers",
        sa.column("code", sa.String),
        sa.column("family", sa.String),
        sa.column("label", sa.String),
        sa.column("enabled", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        brokers_tbl,
        [{
            "code": "mofid",
            "family": "mofid",
            "label": "Mofid (Orbis)",
            "enabled": True,
            "sort_order": 30,
        }],
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM brokers WHERE code = 'mofid' AND family = 'mofid'"))
