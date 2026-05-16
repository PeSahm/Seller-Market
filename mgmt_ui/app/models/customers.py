from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
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


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint("side IN (1, 2)", name="ck_customers_side"),
        sa.UniqueConstraint(
            "agent_id",
            "username",
            "broker",
            "isin",
            "side",
            name="uq_customers_agent_account_broker_isin_side",
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
    section_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    broker: Mapped[str] = mapped_column(String(255), nullable=False)
    isin: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
