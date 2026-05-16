from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import Enum as SAEnum, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


agent_stack_status_enum = SAEnum(
    "provisioning",
    "up",
    "down",
    "deprovisioning",
    name="agent_stack_status",
    create_type=False,
)


class AgentStack(Base):
    __tablename__ = "agent_stacks"
    __table_args__ = (
        UniqueConstraint("server_id", "agent_id", name="uq_agent_stacks_server_agent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    server_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("servers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    stack_dir: Mapped[str] = mapped_column(String(512), nullable=False)
    compose_project: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(agent_stack_status_enum, nullable=False)
    deployed_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )
