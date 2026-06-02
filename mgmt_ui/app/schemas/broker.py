"""Pydantic schemas for the UI-managed ``brokers`` table.

``family`` is a closed ``Literal`` because each family is bound to an adapter in
code — adding a new family is a code change, not a free-text DB value. ``code``
is immutable after create (customers reference it), so :class:`BrokerUpdate`
cannot change it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# The broker families we have adapters for. Extend only alongside a new adapter.
BrokerFamily = Literal["ephoenix", "exir"]

# Broker codes are lowercased + trimmed so "Khobregan" and "khobregan " collide
# on the UNIQUE index instead of creating a dup the dropdown can't tell apart.
# Normalization (strip + lower) runs BEFORE the length check, so a
# whitespace-only code (e.g. "   ") fails min_length=1 instead of slipping
# through and collapsing to "".
NormalizedBrokerCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, min_length=1, max_length=64
    ),
]


class BrokerCreate(BaseModel):
    code: NormalizedBrokerCode
    family: BrokerFamily
    label: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    sort_order: int = 0


class BrokerUpdate(BaseModel):
    # No ``code`` — it's immutable (customers reference it by value).
    label: Optional[str] = Field(default=None, min_length=1, max_length=255)
    family: Optional[BrokerFamily] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None


class BrokerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    family: str
    label: str
    enabled: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime
