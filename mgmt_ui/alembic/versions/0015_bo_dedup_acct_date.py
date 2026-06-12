"""broker_orders: dedup key gains account + placement date

Revision ID: 0015_bo_dedup_acct_date
Revises: 0014_as_reload_status
Create Date: 2026-06-12 02:00:00.000000

ephoenix ``trackingNumber`` turned out to be a small broker-DAY sequence number
(48, 984, 1126, …) that repeats across accounts AND across days — live data
showed 132 rows whose ``raw_json`` ISIN no longer matches the stored ``isin``
(another customer's refresh hit ``ON CONFLICT (broker, tracking_number) DO
UPDATE`` and overwrote the mutable columns while the identity columns stayed),
plus 21 same-account cross-day self-collisions. Scope the dedup key to
``(broker, account_username, tracking_number, placed_date)``: the same REAL
order re-fetched still lands on its own row (same account/trk/date), while a
different order that merely reuses the day-sequence number can no longer
clobber it.

``placed_date`` is the placement-timestamp date. Stored broker timestamps are
Tehran wall-clock labeled UTC, so ``AT TIME ZONE 'UTC'`` recovers the market
date (same basis the fire-log date reconcile uses). It is immutable after
insert, like ``placed_at``.

Existing data needs no dedupe: the old key already guarantees one row per
``(broker, tracking_number)``, so the 4-column key cannot collide.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# NOTE: alembic_version.version_num is VARCHAR(32) — the revision id MUST be
# <= 32 chars (the original 0009 id blew the cap and crash-looped the
# container; see 0009_broker_orders_tracking_composite.py).
revision: str = "0015_bo_dedup_acct_date"
down_revision: Union[str, Sequence[str], None] = "0014_as_reload_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "uq_broker_orders_broker_tracking"
_NEW = "uq_bo_acct_tracking_date"


def upgrade() -> None:
    op.add_column(
        "broker_orders", sa.Column("placed_date", sa.Date(), nullable=True)
    )
    # Backfill from the placement-timestamp chain (live data has zero NULL
    # placed_at; the sentinel keeps the NOT NULL safe regardless).
    op.execute(
        sa.text(
            "UPDATE broker_orders SET placed_date = COALESCE("
            "(placed_at AT TIME ZONE 'UTC')::date, "
            "(created_at_broker AT TIME ZONE 'UTC')::date, "
            "(execution_date AT TIME ZONE 'UTC')::date, "
            "DATE '1970-01-01')"
        )
    )
    op.alter_column("broker_orders", "placed_date", nullable=False)

    op.create_unique_constraint(
        _NEW,
        "broker_orders",
        ["broker", "account_username", "tracking_number", "placed_date"],
    )

    # Drop the old per-broker key, whatever it is named (inspect to be robust —
    # same pattern as 0009).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for uc in insp.get_unique_constraints("broker_orders"):
        if uc.get("column_names") == ["broker", "tracking_number"]:
            op.drop_constraint(uc["name"], "broker_orders", type_="unique")
    for idx in insp.get_indexes("broker_orders"):
        if idx.get("unique") and idx.get("column_names") == ["broker", "tracking_number"]:
            op.drop_index(idx["name"], table_name="broker_orders")


def downgrade() -> None:
    op.drop_constraint(_NEW, "broker_orders", type_="unique")

    # Preflight: under the 4-column key, the same (broker, tracking_number) may
    # legitimately exist on multiple rows (different accounts/days). Recreating
    # the old 2-column UNIQUE would then fail mid-migration; block with a clear
    # message instead (same shape as 0009's downgrade guard).
    bind = op.get_bind()
    dupes = bind.execute(sa.text(
        "SELECT count(*) FROM (SELECT broker, tracking_number FROM broker_orders "
        "GROUP BY broker, tracking_number HAVING count(*) > 1) d"
    )).scalar()
    if dupes:
        from alembic import util
        raise util.CommandError(
            f"Cannot recreate UNIQUE(broker, tracking_number): {dupes} "
            "(broker, tracking_number) pair(s) are duplicated across "
            "accounts/dates. Dedupe before downgrading."
        )

    op.create_unique_constraint(
        _OLD, "broker_orders", ["broker", "tracking_number"]
    )
    op.drop_column("broker_orders", "placed_date")
