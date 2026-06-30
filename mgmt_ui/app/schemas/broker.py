"""Pydantic schemas for the UI-managed ``brokers`` table.

``family`` is a closed ``Literal`` because each family is bound to an adapter in
code — adding a new family is a code change, not a free-text DB value. ``code``
is immutable after create (customers reference it), so :class:`BrokerUpdate`
cannot change it.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# The broker families we have adapters for. Extend only alongside a new adapter.
BrokerFamily = Literal["ephoenix", "exir", "onlineplus", "mofid"]

# A bare DNS-ish domain like ``dnovinbr.ir`` / ``hafezbroker.ir`` — at least one
# dot, no scheme/slashes/spaces. Used for the OnlinePlus per-broker base domain.
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)+$")


def _normalize_base_domain(v: object) -> Optional[str]:
    """Strip/lowercase a base domain; empty -> None; reject a scheme/slash/space
    or a non-domain shape so a pasted full URL can't silently break host
    derivation (the operator should enter just the domain, e.g. ``dnovinbr.ir``)."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    if "://" in s or "/" in s or " " in s or not _DOMAIN_RE.match(s):
        raise ValueError(
            "base_domain must be a bare domain like 'dnovinbr.ir' "
            "(no https://, no path)"
        )
    return s

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
    # OnlinePlus only: the tenant's bare base domain (e.g. "dnovinbr.ir").
    base_domain: Optional[str] = None

    _v_base_domain = field_validator("base_domain", mode="before")(_normalize_base_domain)


class BrokerUpdate(BaseModel):
    # No ``code`` — it's immutable (customers reference it by value).
    label: Optional[str] = Field(default=None, min_length=1, max_length=255)
    family: Optional[BrokerFamily] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None
    base_domain: Optional[str] = None

    _v_base_domain = field_validator("base_domain", mode="before")(_normalize_base_domain)


class BrokerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    family: str
    label: str
    enabled: bool
    sort_order: int
    base_domain: Optional[str] = None
    created_at: datetime
    updated_at: datetime
