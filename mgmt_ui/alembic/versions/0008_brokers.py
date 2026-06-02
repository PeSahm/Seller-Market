"""brokers — UI-managed broker registry + seed

Revision ID: 0008_brokers
Revises: 0007_order_fires
Create Date: 2026-06-02 13:00:00.000000

Replaces the historical hardcoded broker lists (the bot's ``BrokerCode`` enum,
the ``schemas.customer.BROKERS`` tuple/``Literal``, and the Jinja dropdowns)
with a single DB-backed table the operator can CRUD from the admin UI.

``code`` is the immutable key referenced (by value, not FK) from
``customers.broker`` and ``broker_orders.broker``. We seed the 11 existing
ephoenix brokers + the one Exir tenant, then back-fill any DISTINCT broker
value already present in ``customers`` / ``broker_orders`` that the seed list
doesn't cover (so existing rows aren't orphaned when validation turns on).

Purely additive — downgrade drops the table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
# "0008_brokers" is 12 chars — within the VARCHAR(32) cap.
revision: str = "0008_brokers"
down_revision: Union[str, Sequence[str], None] = "0007_order_fires"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (code, label) for the 11 ephoenix brokers, in display order (sort_order 0..10).
_EPHOENIX_SEED: list[tuple[str, str]] = [
    ("gs", "Ghadir Shahr (Ganjine)"),
    ("shahr", "Shahr"),
    ("bbi", "Bourse Bazar Iran"),
    ("karamad", "Karamad"),
    ("tejarat", "Tejarat"),
    ("ebb", "EBB"),
    ("ib", "IbTrader"),
    ("hbc", "Hamerz"),
    ("rabin", "Rabin"),
    ("ayandeh", "Ayandeh"),
    ("farabi", "Farabi"),
]


def upgrade() -> None:
    op.create_table(
        "brokers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("family", sa.String(32), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "sort_order",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_brokers_code", "brokers", ["code"], unique=True
    )

    # ---- Seed the known brokers ------------------------------------------
    # A lightweight table reference for the bulk insert. We omit
    # created_at/updated_at so the column server_default (now()) fills them.
    brokers_tbl = sa.table(
        "brokers",
        sa.column("code", sa.String),
        sa.column("family", sa.String),
        sa.column("label", sa.String),
        sa.column("enabled", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )

    seed_rows: list[dict] = [
        {
            "code": code,
            "family": "ephoenix",
            "label": label,
            "enabled": True,
            "sort_order": idx,
        }
        for idx, (code, label) in enumerate(_EPHOENIX_SEED)
    ]
    # The single Exir tenant.
    seed_rows.append(
        {
            "code": "khobregan",
            "family": "exir",
            "label": "Khobregan Saham (Exir)",
            "enabled": True,
            "sort_order": 20,
        }
    )
    op.bulk_insert(brokers_tbl, seed_rows)

    # ---- Back-fill any broker codes already in use but not seeded --------
    # Existing customers / broker_orders reference brokers by value. If a row
    # references a code the seed list doesn't include, insert it (family
    # 'ephoenix' as the safe default, sort_order 99) so nothing is orphaned
    # when DB-backed broker validation turns on.
    seeded_codes = {r["code"] for r in seed_rows}
    bind = op.get_bind()
    existing = bind.execute(
        sa.text(
            "SELECT DISTINCT broker FROM customers "
            "WHERE broker IS NOT NULL "
            "UNION "
            "SELECT DISTINCT broker FROM broker_orders "
            "WHERE broker IS NOT NULL"
        )
    ).all()

    backfill: list[dict] = []
    seen: set[str] = set()
    for (raw,) in existing:
        if raw is None:
            continue
        code = str(raw).strip().lower()
        if not code or code in seeded_codes or code in seen:
            continue
        seen.add(code)
        backfill.append(
            {
                "code": code,
                "family": "ephoenix",
                "label": code,
                "enabled": True,
                "sort_order": 99,
            }
        )
    if backfill:
        op.bulk_insert(brokers_tbl, backfill)


def downgrade() -> None:
    op.drop_index("ix_brokers_code", table_name="brokers")
    op.drop_table("brokers")
