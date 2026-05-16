"""Pydantic schemas for agent CRUD (Phase 3).

Agents are :class:`~app.models.users.User` rows with ``role='agent'``. The
admin UI creates and lists them via the schemas here. We keep these models
intentionally small — there's no "edit" form in Phase 3, only create / list /
view / soft-delete.

Secret hygiene
--------------
:attr:`AgentCreate.password` is write-only. It is hashed via bcrypt in the
service layer and the plaintext NEVER survives the request. :class:`AgentOut`
deliberately omits ``password_hash`` so it can't be leaked via a JSON
response, an audit-log payload, or an HTML template that happens to dump the
dict.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AgentCreate(BaseModel):
    """Inbound payload for the "Add agent" admin form.

    ``telegram_user_id`` is optional today — agents who don't have Telegram
    configured can still log in via username/password. Phase 4 will wire it
    into the bot side.
    """

    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    telegram_user_id: Optional[str] = Field(default=None, max_length=64)


class AgentOut(BaseModel):
    """Outbound representation of an agent (a User row with role='agent').

    Deliberately omits ``password_hash`` and ``role``. The caller already
    knows the role is ``'agent'`` (that's how it found this row) and the
    password hash MUST NOT round-trip through any HTML or JSON surface.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    telegram_user_id: Optional[str]
    deleted_at: Optional[datetime]
    created_at: datetime
