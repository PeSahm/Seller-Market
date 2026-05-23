"""split customers into customers + trade_instructions

Revision ID: 0003_split_customer_trade_instruction
Revises: 0002_server_image_pull_policy
Create Date: 2026-05-23 12:00:00.000000

Today the ``customers`` table conflates two concepts:

* the **brokerage account** (agent_id, broker, username, password) — the thing
  whose credentials we need to log in;
* the **tradeable instrument** (isin, side) — what to actually trade on
  that account.

Operators with 10 instruments per account were forced to duplicate the
credential set ten times. This migration splits the two:

* ``customers`` keeps everything account-shaped (drops section_name, isin,
  side, comment). The OLD composite UNIQUE
  ``(agent_id, username, broker, isin, side)`` becomes
  ``(agent_id, broker, username)`` — one credential set per account.
* New ``trade_instructions`` table carries the per-instrument fields:
  ``(customer_id, isin, side, section_name, comment, enabled)`` with
  UNIQUE ``(customer_id, isin, side)``.

Data shuffle inside upgrade():

1. Each existing customer row becomes one ``trade_instruction`` row,
   pointing at the *canonical* customer for its ``(agent_id, broker,
   username)`` group. Canonical = the one with the earliest
   ``created_at`` (deterministic tie-break by ``id`` for safety).
2. Any ``trade_results`` rows that referenced a non-canonical customer
   are repointed to the canonical customer (they keep their own
   ``isin``/``side`` columns so per-trade granularity isn't lost; the
   association just moves to the account-level Customer).
3. Non-canonical customer rows are deleted; the canonical one is
   stripped of the per-trade columns; the new constraint is added.

Bot-facing config.ini is **unchanged in shape** — the renderer's join
of customers × trade_instructions still emits one section per
``(username, broker, isin, side)`` tuple, same as before.

Downgrade is intentionally not supported. After consolidation the
non-canonical Customer ids are gone forever; you'd be inventing them.
If a rollback is needed in production, restore from a backup snapshot
taken right before alembic upgrade.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0003_split_customer_trade_instruction"
down_revision: Union[str, Sequence[str], None] = "0002_server_image_pull_policy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ----- 1. Create the new trade_instructions table ------------------
    op.create_table(
        "trade_instructions",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("isin", sa.String(64), nullable=False),
        sa.Column("side", sa.Integer, nullable=False),
        sa.Column("section_name", sa.String(255), nullable=False, unique=True),
        sa.Column(
            "enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column(
            "version",
            sa.Integer,
            nullable=False,
            server_default=sa.text("1"),
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
        sa.CheckConstraint("side IN (1, 2)", name="ck_trade_instructions_side"),
        sa.UniqueConstraint(
            "customer_id",
            "isin",
            "side",
            name="uq_trade_instructions_customer_isin_side",
        ),
    )

    bind = op.get_bind()

    # ----- 2. One trade_instruction per existing customer row ----------
    # Canonical customer per (agent_id, broker, username) tuple is the one
    # with the earliest ``created_at`` (DISTINCT ON in PG). Every old row
    # becomes one trade_instruction pointing at that canonical customer.
    bind.execute(
        sa.text(
            """
            WITH canonical AS (
                SELECT DISTINCT ON (agent_id, broker, username)
                       id AS canonical_id, agent_id, broker, username
                FROM customers
                -- ``enabled DESC`` picks an enabled row over a disabled one
                -- if the group has both — otherwise the consolidated Customer
                -- inherits ``enabled=False`` even though some of its
                -- trade instructions are still meant to be active.
                ORDER BY agent_id, broker, username, enabled DESC, created_at ASC, id ASC
            )
            INSERT INTO trade_instructions (
                id, customer_id, isin, side, section_name, comment,
                enabled, version, created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                can.canonical_id,
                c.isin,
                c.side,
                c.section_name,
                c.comment,
                c.enabled,
                1,
                c.created_at,
                c.updated_at
            FROM customers c
            JOIN canonical can ON
                can.agent_id = c.agent_id
                AND can.broker = c.broker
                AND can.username = c.username
            """
        )
    )

    # ----- 3. Repoint trade_results from non-canonical to canonical ----
    # trade_results.customer_id FK is RESTRICT, so we can't delete a
    # non-canonical customer while trade_results still reference it.
    bind.execute(
        sa.text(
            """
            WITH canonical AS (
                SELECT DISTINCT ON (agent_id, broker, username)
                       id AS canonical_id, agent_id, broker, username
                FROM customers
                -- ``enabled DESC`` picks an enabled row over a disabled one
                -- if the group has both — otherwise the consolidated Customer
                -- inherits ``enabled=False`` even though some of its
                -- trade instructions are still meant to be active.
                ORDER BY agent_id, broker, username, enabled DESC, created_at ASC, id ASC
            )
            UPDATE trade_results tr
            SET customer_id = can.canonical_id
            FROM customers c
            JOIN canonical can ON
                can.agent_id = c.agent_id
                AND can.broker = c.broker
                AND can.username = c.username
            WHERE tr.customer_id = c.id
              AND c.id <> can.canonical_id
            """
        )
    )

    # ----- 4. Delete non-canonical customer rows -----------------------
    bind.execute(
        sa.text(
            """
            WITH canonical AS (
                SELECT DISTINCT ON (agent_id, broker, username)
                       id AS canonical_id, agent_id, broker, username
                FROM customers
                -- ``enabled DESC`` picks an enabled row over a disabled one
                -- if the group has both — otherwise the consolidated Customer
                -- inherits ``enabled=False`` even though some of its
                -- trade instructions are still meant to be active.
                ORDER BY agent_id, broker, username, enabled DESC, created_at ASC, id ASC
            )
            DELETE FROM customers c
            USING canonical can
            WHERE c.agent_id = can.agent_id
              AND c.broker = can.broker
              AND c.username = can.username
              AND c.id <> can.canonical_id
            """
        )
    )

    # ----- 5. Drop the old composite UNIQUE + CHECK + per-trade cols ---
    op.drop_constraint(
        "uq_customers_agent_account_broker_isin_side",
        "customers",
        type_="unique",
    )
    op.drop_constraint("ck_customers_side", "customers", type_="check")
    op.drop_column("customers", "section_name")
    op.drop_column("customers", "isin")
    op.drop_column("customers", "side")
    op.drop_column("customers", "comment")

    # ----- 6. Add the new account-level UNIQUE -------------------------
    op.create_unique_constraint(
        "uq_customers_agent_broker_username",
        "customers",
        ["agent_id", "broker", "username"],
    )


def downgrade() -> None:
    # The migration consolidates duplicate (agent_id, broker, username) Customer
    # rows into one. The non-canonical Customer ids are dropped during step 4
    # of upgrade — you can't reconstruct them from trade_instructions alone.
    # If you ever need to roll back in production, restore from the backup
    # snapshot taken right before ``alembic upgrade``.
    raise NotImplementedError(
        "0003 split is one-way (non-canonical Customer ids are dropped during "
        "consolidation). Restore from a pre-upgrade DB snapshot to roll back."
    )
