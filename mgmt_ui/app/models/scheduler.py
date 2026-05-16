from __future__ import annotations

import uuid
from datetime import time

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


scheduler_job_name_enum = SAEnum(
    "cache_warmup",
    "run_trading",
    name="scheduler_job_name",
    create_type=False,
)


class SchedulerJob(Base):
    __tablename__ = "scheduler_jobs"
    __table_args__ = (
        UniqueConstraint("stack_id", "name", name="uq_scheduler_jobs_stack_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    stack_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_stacks.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(scheduler_job_name_enum, nullable=False)
    time: Mapped[time] = mapped_column(Time, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    command: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class LocustConfig(Base):
    __tablename__ = "locust_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    stack_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_stacks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    users: Mapped[int] = mapped_column(Integer, nullable=False)
    spawn_rate: Mapped[int] = mapped_column(Integer, nullable=False)
    run_time: Mapped[str] = mapped_column(String(64), nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    processes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
