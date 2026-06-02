"""The ``brokers`` table — UI-managed broker registry.

Replaces the historical hardcoded broker lists (the bot's ``BrokerCode`` enum,
the ``schemas.customer.BROKERS`` tuple/``Literal``, and the Jinja dropdowns).
Each row is a broker the operator can trade through; ``family`` selects which
adapter/URL-shape it uses (``ephoenix`` or ``exir``).

``code`` is the immutable key referenced (by value, not FK) from
``customers.broker`` and ``broker_orders.broker``. Migration ``0008`` seeds the
11 existing ephoenix brokers + the Exir tenant(s) so no existing row is orphaned
when DB-backed validation turns on. Adding a broker within a known family is a
pure DB insert — no image rebuild.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class Broker(Base):
    __tablename__ = "brokers"
    # Enforce canonical (lowercase) codes at the DB boundary so a mixed-case
    # value can never slip in via a path that skips the schema validator and
    # silently defeat exact-match routing (``registry.family_of(code)``).
    __table_args__ = (
        sa.CheckConstraint("code = lower(code)", name="ck_brokers_code_lowercase"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # The broker code: ephoenix codes ("gs", "ib", ...) OR an Exir tenant
    # subdomain ("khobregan"). Immutable after create (customers reference it by
    # value). UNIQUE so the dropdown/validation has a single source of truth.
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # "ephoenix" | "exir" — selects the adapter + URL shape. Enforced as a
    # Literal in the schema layer (families are code-bound: each needs an
    # adapter, so adding a family is a code change, not a DB row).
    family: Mapped[str] = mapped_column(String(32), nullable=False)
    # Human-friendly label shown in dropdowns (e.g. "Ghadir Shahr (Ganjine)").
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    # Disabled brokers stay in the table (and keep validating existing customers)
    # but drop out of the create-customer dropdown.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
