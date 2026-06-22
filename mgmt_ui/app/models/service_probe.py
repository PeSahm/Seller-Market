"""Per-server service reachability probe results (the ``/admin/server-services`` matrix).

A background worker SSH-probes every managed server for every service it depends
on (OCR, ephoenix per broker + the shared ``marketdatagw``, ibtrader, exir
tenants, RLC, the market-data sidecar) and stores the latest result here — one
row per ``(server_id, target_key)``. The probe runs FROM each server (so it
reflects that server's own network reachability — the lesson from
``marketdatagw`` being reachable from Tebyan but not PouyanIt) and classifies a
genuine API apart from a live-but-placeholder host (e.g. the old ``mdapi1`` now
serves plain HTML).

The manual "Deep check" tier (a real authenticated login with Mostafa's
credential, run inside a bot container) writes rows here too, under the
``auth-ephoenix`` / ``auth-exir`` groups.

Composite PK ``(server_id, target_key)`` → a clean upsert key (one latest result
per server per endpoint).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class ServiceProbeResult(Base):
    __tablename__ = "service_probe_results"

    server_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("servers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Stable per-endpoint key, e.g. ``ephoenix:identity:ayandeh``,
    # ``ephoenix:marketdatagw``, ``ocr:5.10.248.55:18080``, ``rlc:core``,
    # ``auth:ayandeh``, or the synthetic ``_meta:__ssh__``.
    target_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Service group for the matrix: ocr | market-data | ephoenix |
    # ephoenix-legacy | ibtrader | exir | rlc | auth-ephoenix | auth-exir | _meta.
    group_name: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # real | up | placeholder | degraded | down | skipped.
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Short note: body marker, error first ~120 chars, or "host unreachable (ssh)".
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    probed_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )
