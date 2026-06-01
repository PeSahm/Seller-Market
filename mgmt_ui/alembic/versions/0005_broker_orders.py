"""broker_orders — direct GetOrders history for the fee report

Revision ID: 0005_broker_orders
Revises: 0004_drop_enabled
Create Date: 2026-06-01 18:00:00.000000

The mgmt UI's only trade source was ``order_results/*.json`` SFTP'd from the
bot, written from ``GetOpenOrders`` — which never returns already-completed
orders, so finished trades were silently lost (the /admin/trades "misses
trades" bug). This table is the mgmt UI's OWN source of truth, populated by
calling the broker ``GetOrders`` API directly (``includeStatus:[3]``).

It is deliberately separate from ``trade_results``:

* no ``run_id`` (GetOrders rows aren't tied to a mgmt-UI run);
* stores sells too (no TradeInstruction gate), needed for buy↔sell profit
  matching;
* carries fee/value columns (``total_fee``, ``executed_amount``,
  ``net_traded_value``, ``pam_code``) the report needs.

Dedup on ``tracking_number`` (UNIQUE) with ON CONFLICT DO UPDATE in the
reconciler, since GetOrders polls mutable broker state.

Downgrade is a clean ``drop_table`` (purely additive migration).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
# NOTE: ``alembic_version.version_num`` is ``VARCHAR(32)`` — keep this short
# (PR #89's hard-learned lesson). "0005_broker_orders" is 18 chars.
revision: str = "0005_broker_orders"
down_revision: Union[str, Sequence[str], None] = "0004_drop_enabled"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "broker_orders",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
            index=True,
        ),
        sa.Column("broker", sa.String(255), nullable=False),
        sa.Column("account_username", sa.String(255), nullable=False),
        sa.Column("pam_code", sa.String(64), nullable=True),
        sa.Column("tracking_number", sa.BigInteger, nullable=False, unique=True),
        sa.Column("broker_order_id", sa.BigInteger, nullable=True),
        sa.Column("isin", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=True),
        sa.Column("symbol_title", sa.String(128), nullable=True),
        sa.Column("order_side", sa.Integer, nullable=False),
        sa.Column("price", sa.Numeric(20, 4), nullable=True),
        sa.Column("volume", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "executed_volume",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("total_fee", sa.Numeric(24, 4), nullable=True),
        sa.Column("executed_amount", sa.Numeric(24, 4), nullable=True),
        sa.Column("net_traded_value", sa.Numeric(24, 4), nullable=True),
        sa.Column("state", sa.Integer, nullable=False),
        sa.Column("state_desc", sa.Text, nullable=True),
        sa.Column(
            "is_done", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at_broker", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("execution_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "is_bot", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column("raw_json", postgresql.JSONB, nullable=False),
        sa.Column(
            "first_seen_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_broker_orders_customer_isin_side_state",
        "broker_orders",
        ["customer_id", "isin", "order_side", "state"],
    )
    op.create_index("ix_broker_orders_placed_at", "broker_orders", ["placed_at"])
    op.create_index(
        "ix_broker_orders_agent_id_placed_at",
        "broker_orders",
        ["agent_id", "placed_at"],
    )


def downgrade() -> None:
    # Purely additive — a real drop is safe (unlike 0003/0004).
    op.drop_index("ix_broker_orders_agent_id_placed_at", table_name="broker_orders")
    op.drop_index("ix_broker_orders_placed_at", table_name="broker_orders")
    op.drop_index(
        "ix_broker_orders_customer_isin_side_state", table_name="broker_orders"
    )
    op.drop_table("broker_orders")
