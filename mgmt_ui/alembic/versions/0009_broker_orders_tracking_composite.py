"""broker_orders: scope the dedup key to (broker, tracking_number)

Revision ID: 0009_broker_orders_tracking_composite
Revises: 0008_brokers
Create Date: 2026-06-02 14:00:00.000000

``broker_orders.tracking_number`` was globally UNIQUE — a safe assumption while
every row came from one ephoenix platform id namespace. The Exir family writes
``mmtpOrderId`` (an INDEPENDENT per-tenant namespace) into the same column, so a
global unique would let an Exir id collide with — and via ``ON CONFLICT DO
UPDATE`` silently overwrite — a different broker's / customer's money +
attribution row (and vice-versa). Scope the dedup key to ``(broker,
tracking_number)`` instead, which is what "unique order id" actually means here.

Existing data is unaffected: every previously-global-unique ``tracking_number``
is trivially unique within its broker too, so the new composite constraint is
strictly more permissive.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009_broker_orders_tracking_composite"
down_revision: Union[str, Sequence[str], None] = "0008_brokers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COMPOSITE = "uq_broker_orders_broker_tracking"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # Drop the existing single-column UNIQUE on tracking_number, whatever it is
    # named (Postgres auto-names an inline column UNIQUE
    # ``broker_orders_tracking_number_key``; inspect to be robust).
    for uc in insp.get_unique_constraints("broker_orders"):
        if uc.get("column_names") == ["tracking_number"]:
            op.drop_constraint(uc["name"], "broker_orders", type_="unique")
    for idx in insp.get_indexes("broker_orders"):
        if idx.get("unique") and idx.get("column_names") == ["tracking_number"]:
            op.drop_index(idx["name"], table_name="broker_orders")

    op.create_unique_constraint(
        _COMPOSITE, "broker_orders", ["broker", "tracking_number"]
    )


def downgrade() -> None:
    op.drop_constraint(_COMPOSITE, "broker_orders", type_="unique")
    # Restore the original single-column UNIQUE (Postgres default name).
    op.create_unique_constraint(
        "broker_orders_tracking_number_key", "broker_orders", ["tracking_number"]
    )
