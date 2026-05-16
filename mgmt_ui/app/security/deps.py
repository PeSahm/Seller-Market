from __future__ import annotations

import uuid
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.users import User
from app.security.auth import decode_token

# auto_error=False so we can fall back to cookie auth.
bearer_scheme = HTTPBearer(auto_error=False)

COOKIE_NAME = "access_token"


def _extract_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    """Pull a JWT from the access_token cookie first, then a Bearer header."""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        return token
    if credentials and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    return None


async def _load_user(db: AsyncSession, subject: str) -> Optional[User]:
    """Look up a user by id (subject). Returns None if not found or deleted."""
    try:
        user_id = uuid.UUID(subject)
    except (ValueError, TypeError):
        return None
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        return None
    if user.deleted_at is not None:
        return None
    return user


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the current user from cookie or bearer token.

    Raises 401 if no token, the token is invalid/expired, the user is
    missing, or the user is soft-deleted.
    """
    token = _extract_token(request, credentials)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    subject = payload.get("sub")
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await _load_user(db, subject)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or has been removed",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Allow only admin users."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user


def require_agent(user: User = Depends(get_current_user)) -> User:
    """Allow agents and admins (admins can act as agents)."""
    if user.role not in ("agent", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent privileges required",
        )
    return user


async def ws_authenticate(token: str, db: AsyncSession) -> User:
    """Authenticate a WebSocket connection from a query-param token.

    Raises ``HTTPException(401)`` on failure; the caller should translate
    that into ``websocket.close(code=4401)``.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
        )
    try:
        payload = decode_token(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    subject = payload.get("sub")
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    user = await _load_user(db, subject)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or has been removed",
        )
    return user
