"""drop enabled column + hard-delete phantom trade_instructions

Revision ID: 0004_drop_enabled
Revises: 0003_split_customer_ti
Create Date: 2026-05-23 13:30:00.000000

The 0001-era soft-delete pattern (flip ``enabled=False``, keep the row)
was confusing operators: a "deleted" row stayed in the DB, occupied the
composite UNIQUE slot, required a "Show disabled" toggle to find, and
needed a follow-up hard-delete via the DB to truly remove. Per the
operator-facing redesign, we move to:

* Customer  — no delete at all. Long-lived account record.
* TradeInstruction — hard delete (row gone from DB).

This migration just drops the ``enabled`` column from both tables.
Phantom TIs (rows with ``enabled=False`` left over from old
soft-deletes) are hard-deleted as part of step 1 — that's exactly what
they were meant to be by the operator who clicked Delete on them.

Customer rows are NOT touched: PR #88's canonical-selection already
preferred enabled customers, so the live DB has no disabled customers
to clean up. We just drop the column.

Downgrade is one-way (same convention as 0003): the deleted phantom
TIs can't be reconstructed.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
# NOTE: ``alembic_version.version_num`` is ``VARCHAR(32)`` — keep this
# string short (see PR #89 for the hard-learned lesson).
revision: str = "0004_drop_enabled"
down_revision: Union[str, Sequence[str], None] = "0003_split_customer_ti"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Hard-delete any phantom (disabled) trade_instructions. These were
    #    "deleted" by the old soft-delete code path and have been sitting
    #    around blocking the (customer_id, isin, side) UNIQUE slot ever
    #    since.
    op.execute("DELETE FROM trade_instructions WHERE enabled = FALSE")

    # 2. Drop the enabled column from both tables. The cross-join in the
    #    renderer no longer needs to filter on it (everything that exists
    #    is meant to be rendered).
    op.drop_column("trade_instructions", "enabled")
    op.drop_column("customers", "enabled")


def downgrade() -> None:
    # The deleted phantom rows can't be reconstructed (we don't know
    # which TIs were enabled vs. disabled before the upgrade). If a
    # rollback is ever needed, restore from the pre-upgrade backup
    # snapshot.
    raise NotImplementedError(
        "0004 is one-way (phantom TIs were hard-deleted). "
        "Restore from a pre-upgrade DB snapshot to roll back."
    )
