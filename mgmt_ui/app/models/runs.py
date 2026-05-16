from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import (
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


run_job_name_enum = SAEnum(
    "cache_warmup",
    "run_trading",
    name="run_job_name",
    create_type=False,
)

run_trigger_enum = SAEnum(
    "scheduled",
    "manual",
    "api",
    "retry",
    name="run_trigger",
    create_type=False,
)

run_status_enum = SAEnum(
    "running",
    "success",
    "failed",
    "killed",
    name="run_status",
    create_type=False,
)

stack_run_lock_kind_enum = SAEnum(
    "cache",
    "trade",
    name="stack_run_lock_kind",
    create_type=False,
)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    stack_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_stacks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    job_name: Mapped[str] = mapped_column(run_job_name_enum, nullable=False)
    trigger: Mapped[str] = mapped_column(run_trigger_enum, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(run_status_enum, nullable=False)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    log_blob_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    log_blob_sha256: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)


class StackRunLock(Base):
    __tablename__ = "stack_run_locks"

    stack_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_stacks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(stack_run_lock_kind_enum, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    lease_expires_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
    )
    holder: Mapped[str] = mapped_column(String(255), nullable=False)


class IngestCursor(Base):
    __tablename__ = "ingest_cursors"

    stack_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_stacks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_filename: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    last_mtime: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
