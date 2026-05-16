from __future__ import annotations

import uuid
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """Credentials posted to /auth/login."""

    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=1024)


class TokenResponse(BaseModel):
    """OAuth2-style bearer token response."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # seconds until token expiry


class UserOut(BaseModel):
    """Public representation of a User."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    role: Literal["admin", "agent"]
    telegram_user_id: Optional[str] = None
