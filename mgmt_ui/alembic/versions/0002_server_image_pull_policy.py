"""add servers.image_pull_policy column (issue #71-incremental)

Revision ID: 0002_server_image_pull_policy
Revises: 0001_init
Create Date: 2026-05-19 19:30:00.000000

Adds a per-server pull policy that controls whether the mgmt UI's
``redeploy_stack`` includes ``--pull always`` on its
``docker compose up -d``. Hosts in restricted-egress environments
(Iranian VPSes where ``ghcr.io`` is blocked) can be flipped to
``never`` so the operator's pre-pull + retag from a mirror is the only
fetch that ever runs.

``always`` is the default — every existing server keeps its current
behaviour unchanged after the migration.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0002_server_image_pull_policy"
down_revision: Union[str, Sequence[str], None] = "0001_init"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Native Postgres ENUM. Mirrors the SAEnum on ``app.models.servers``.
server_image_pull_policy = postgresql.ENUM(
    "always",
    "missing",
    "never",
    name="server_image_pull_policy",
)


def upgrade() -> None:
    # Create the enum type FIRST. Without ``create_type=False`` we'd get
    # "type already exists" on re-runs, but it doesn't exist yet, so it
    # must be created here. ``checkfirst=True`` keeps the migration
    # idempotent for partial re-applies.
    server_image_pull_policy.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "servers",
        sa.Column(
            "image_pull_policy",
            server_image_pull_policy,
            nullable=False,
            server_default=sa.text("'always'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("servers", "image_pull_policy")
    server_image_pull_policy.drop(op.get_bind(), checkfirst=True)
