from __future__ import annotations

from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class MgmtInstance(Base):
    """A heartbeat row per running mgmt instance (#156 HA visibility).

    Each instance upserts its own row every ~15s to the SHARED database, so any
    instance's ``/admin/ha`` page can list all instances (PouyanIt, ParsPack, …)
    with which one holds worker leadership and which DB each is serving.
    """

    __tablename__ = "mgmt_instances"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    active_db: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=sa.text("'main'")
    )
    is_leader: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa.text("false")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
    )
