from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import (
    Enum as SAEnum,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


customer_assignment_status_enum = SAEnum(
    "pending",
    "assigned",
    "active",
    name="customer_assignment_status",
    create_type=False,
)

distribution_scope_enum = SAEnum(
    "global",
    "agent",
    name="distribution_scope",
    create_type=False,
)

distribution_policy_enum = SAEnum(
    "manual",
    "round_robin",
    "least_customers",
    "broker_affinity",
    name="distribution_policy",
    create_type=False,
)

# Per-customer credential health. ``unknown`` = never checked (existing rows /
# before the first verify); ``valid``/``invalid`` = high-confidence verdicts;
# ``transient`` = the last check couldn't decide (OCR/broker down) — kept
# distinct from ``unknown`` so a blip doesn't read as "never checked". The
# dashboard "needs attention" metric counts ONLY ``invalid``.
customer_credential_status_enum = SAEnum(
    "unknown",
    "valid",
    "invalid",
    "transient",
    name="customer_credential_status",
    create_type=False,
)


class Customer(Base):
    """A brokerage account: one credential set per (agent, broker, username).

    Post-migration 0003, Customer carries ONLY the account fields. The
    per-instrument fields (isin, side, section_name, comment) moved to
    :class:`TradeInstruction` — one customer can have many.

    The composite UNIQUE ``(agent_id, broker, username)`` enforces "one
    account per agent+broker"; trying to add a second customer with the
    same trio raises an IntegrityError that the service layer translates
    to a friendly ValueError.
    """

    __tablename__ = "customers"
    __table_args__ = (
        sa.UniqueConstraint(
            "agent_id",
            "broker",
            "username",
            name="uq_customers_agent_broker_username",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    server_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("servers.id", ondelete="RESTRICT"),
        nullable=True,
    )
    stack_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_stacks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    assignment_status: Mapped[str] = mapped_column(
        customer_assignment_status_enum,
        nullable=False,
        server_default=text("'pending'"),
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    broker: Mapped[str] = mapped_column(String(255), nullable=False)
    # Per-customer profit-share fee % override (#116). NULL = no override; the
    # report falls back to the agent override → global setting → default.
    fee_percent: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    # Credential health (set by verify-on-save + the daily noon checker). System
    # metadata — NOT bumped by the optimistic-lock ``version`` (so a status write
    # never collides with a concurrent agent edit).
    credential_status: Mapped[str] = mapped_column(
        customer_credential_status_enum,
        nullable=False,
        server_default=text("'unknown'"),
    )
    credential_checked_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    credential_message: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


class DistributionPolicy(Base):
    __tablename__ = "distribution_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    scope: Mapped[str] = mapped_column(distribution_scope_enum, nullable=False)
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    policy: Mapped[str] = mapped_column(distribution_policy_enum, nullable=False)
    default_server_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("servers.id", ondelete="RESTRICT"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
