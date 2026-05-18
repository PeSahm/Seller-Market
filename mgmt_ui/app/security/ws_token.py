"""Short-lived JWT for WebSocket auth (Phase 10).

The WS endpoint /ws/runs/{id} previously authenticated via the same
``access_token`` cookie as HTML routes. That works, but means a
malicious cross-origin page COULD initiate a WebSocket upgrade and
the browser would attach the cookie — CSRF middleware doesn't run on
WS upgrades (no body to read for the form-field check, no preflight).

Mitigation: a short-lived (30 s) signed JWT in the WS URL query
string. The cookie-authenticated user calls ``POST /auth/ws-token``
to mint one, then connects with ``?token=...``. The WS handler
verifies the JWT BEFORE accepting the connection; bare cookie auth
is no longer sufficient.

This is the standard "Sec-WebSocket-Protocol header is awkward in
the browser" pattern — query-string short-TTL JWT is the pragmatic
substitute for native WS auth.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt

from app.settings import get_settings

WS_TOKEN_TTL_SECONDS = 30
WS_TOKEN_PURPOSE = "ws"


def issue_ws_token(user_id: str) -> str:
    """Mint a 30-second JWT bound to ``user_id`` for a WS upgrade."""
    settings = get_settings()
    now = datetime.now(tz=timezone.utc)
    expire = now + timedelta(seconds=WS_TOKEN_TTL_SECONDS)
    payload: dict[str, Any] = {
        "sub": user_id,
        "purpose": WS_TOKEN_PURPOSE,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(
        payload,
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def verify_ws_token(token: str) -> str | None:
    """Return the ``sub`` (user_id) if valid, else None.

    Failure modes (all → None):
    - missing / malformed token
    - bad signature
    - expired (jose checks exp automatically)
    - purpose != "ws" (a stolen access_token can't be re-used as
      a ws-token by virtue of the purpose claim mismatch)
    """
    if not token:
        return None
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError:
        return None
    if payload.get("purpose") != WS_TOKEN_PURPOSE:
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    return sub


__all__ = [
    "WS_TOKEN_TTL_SECONDS",
    "issue_ws_token",
    "verify_ws_token",
]
