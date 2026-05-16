from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import jwt
from passlib.context import CryptContext

from app.settings import get_settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plaintext: str) -> str:
    """Return a bcrypt hash for the given password."""
    return pwd_context.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Check that ``plaintext`` matches the bcrypt ``hashed`` value."""
    try:
        return pwd_context.verify(plaintext, hashed)
    except ValueError:
        # Malformed hash; treat as failed verification.
        return False


def create_access_token(
    subject: str,
    role: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Issue a JWT access token.

    Encodes ``sub`` (user id as string), ``role``, ``iat`` and ``exp``.
    """
    settings = get_settings()
    now = datetime.now(tz=timezone.utc)
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.jwt_expire_minutes)
    expire = now + expires_delta
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(
        payload,
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_token(token: str) -> dict[str, Any]:
    """Decode a JWT, raising ``jose.JWTError`` on failure."""
    settings = get_settings()
    return jwt.decode(
        token,
        settings.secret_key.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )
