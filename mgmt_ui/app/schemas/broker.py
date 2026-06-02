"""Pydantic schemas for the UI-managed ``brokers`` table.

``family`` is a closed ``Literal`` because each family is bound to an adapter in
code — adding a new family is a code change, not a free-text DB value. ``code``
is immutable after create (customers reference it), so :class:`BrokerUpdate`
cannot change it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The broker families we have adapters for. Extend only alongside a new adapter.
BrokerFamily = Literal["ephoenix", "exir"]


def _normalize_code(v: str) -> str:
    """Codes are lowercased + trimmed so "Khobregan" and "khobregan " collide
    on the UNIQUE index instead of creating a dup the dropdown can't tell apart.
    """
    return v.strip().lower()


class BrokerCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    family: BrokerFamily
    label: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    sort_order: int = 0

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        return _normalize_code(v)


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
