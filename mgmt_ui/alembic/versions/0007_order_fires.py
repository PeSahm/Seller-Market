"""order_fires — bot fire-log for authoritative bot-buy attribution

Revision ID: 0007_order_fires
Revises: 0006_agent_fee_configs
Create Date: 2026-06-02 12:00:00.000000

The bot appends one JSONL record per account per run to
``run_results/order_fires_<date>.jsonl`` recording which customer/broker/isin/
side it fired an order for. The mgmt UI ingests these and reconciles them
against ``broker_orders`` to authoritatively tag which executed buys were the
bot's (vs the agent's manual trades), and to surface a daily "which customers
ran" roster.

Idempotent ingest is keyed on the bot-generated ``fire_uid`` (UNIQUE).
Purely additive — downgrade drops the table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
# "0007_order_fires" is 16 chars — within the VARCHAR(32) cap.
revision: str = "0007_order_fires"
down_revision: Union[str, Sequence[str], None] = "0006_agent_fee_configs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "order_fires",
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
        sa.Column("isin", sa.String(64), nullable=False),
        sa.Column("side", sa.Integer, nullable=False),
        sa.Column("fired_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("run_date", sa.Date, nullable=False, index=True),
        sa.Column("fire_uid", sa.String(128), nullable=False, unique=True),
        sa.Column("serial_number", sa.BigInteger, nullable=True),
        sa.Column("tracking_number", sa.BigInteger, nullable=True),
        sa.Column(
            "reconciled", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column("raw_json", postgresql.JSONB, nullable=True),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_order_fires_customer_isin_side",
        "order_fires",
        ["customer_id", "isin", "side"],
    )
    op.create_index(
        "ix_order_fires_serial_number", "order_fires", ["serial_number"]
    )

    # broker_orders gains the broker serial number (from GetOrders.serialNumber)
    # so the fire-log can reconcile a bot fire against its execution by serial.
    op.add_column(
        "broker_orders", sa.Column("serial_number", sa.BigInteger, nullable=True)
    )
    op.create_index(
        "ix_broker_orders_serial_number", "broker_orders", ["serial_number"]
    )


def downgrade() -> None:
    op.drop_index("ix_broker_orders_serial_number", table_name="broker_orders")
    op.drop_column("broker_orders", "serial_number")
    op.drop_index("ix_order_fires_serial_number", table_name="order_fires")
    op.drop_index("ix_order_fires_customer_isin_side", table_name="order_fires")
    op.drop_table("order_fires")
