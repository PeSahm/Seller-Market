from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import Enum as SAEnum, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


ssh_auth_enum = SAEnum(
    "password",
    "pubkey",
    name="ssh_auth",
    create_type=False,
)

server_status_enum = SAEnum(
    "unknown",
    "online",
    "offline",
    name="server_status",
    create_type=False,
)

# Issue #71-incremental: per-server image-pull policy.
#
# ``always`` (default) — current behaviour. ``docker compose up -d`` is
# invoked with ``--pull always`` on redeploy so the trading host fetches
# the latest tag from the registry every time.
#
# ``missing`` — pull only when the image isn't already present locally.
# Mostly useful for staging.
#
# ``never`` — never pull. Designed for hosts where ``ghcr.io`` is blocked
# (Iranian VPSes etc): the operator pre-pulls + retags via a mirror, and
# the mgmt UI's redeploy uses the local image as-is.
image_pull_policy_enum = SAEnum(
    "always",
    "missing",
    "never",
    name="server_image_pull_policy",
    create_type=False,
)


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    ssh_port: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("22"))
    ssh_user: Mapped[str] = mapped_column(String(255), nullable=False)
    ssh_auth: Mapped[str] = mapped_column(ssh_auth_enum, nullable=False)
    ssh_secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    host_key_pin: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        server_status_enum,
        nullable=False,
        server_default=text("'unknown'"),
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )
    base_dir: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        server_default=text("'/root/seller-market/agents'"),
    )
    # See ``image_pull_policy_enum`` above. ``always`` matches the
    # pre-#71 behaviour so existing servers are bytewise unchanged.
    image_pull_policy: Mapped[str] = mapped_column(
        image_pull_policy_enum,
        nullable=False,
        server_default=text("'always'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


class ServerClockSkewSample(Base):
    __tablename__ = "server_clock_skew_samples"

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
    sampled_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    delta_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
