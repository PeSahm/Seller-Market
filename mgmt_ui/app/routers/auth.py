from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.users import User
from app.schemas.auth import LoginRequest, TokenResponse, UserOut
from app.security.auth import create_access_token, verify_password
from app.security.deps import COOKIE_NAME, get_current_user
from app.security.rate_limit import client_key, login_limiter, ws_token_limiter
from app.security.ws_token import WS_TOKEN_TTL_SECONDS, issue_ws_token
from app.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _is_form_post(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    return content_type.startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    )


def _is_htmx_or_form(request: Request) -> bool:
    """Detect HTMX requests or form-encoded posts (vs JSON API clients)."""
    return _is_htmx(request) or _is_form_post(request)


async def _authenticate(db: AsyncSession, username: str, password: str) -> Optional[User]:
    """Validate credentials, returning the active User or None."""
    stmt = select(User).where(User.username == username)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        return None
    if user.deleted_at is not None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def _set_cookie(response: Response, token: str, max_age_seconds: int) -> None:
    settings = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.get("/login", include_in_schema=False)
async def login_form(request: Request):
    """Render the login page."""
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None},
    )


@router.post("/login")
async def login(
    request: Request,
    db: AsyncSession = Depends(get_db),
    username: Optional[str] = Form(default=None),
    password: Optional[str] = Form(default=None),
):
    """Authenticate either via form (HTMX) or JSON body.

    On success:
      - Always sets the ``access_token`` HttpOnly cookie.
      - For HTMX/form clients: returns 204 with ``HX-Redirect: /``.
      - For JSON clients: returns a ``TokenResponse`` body.

    Phase 10 rate limit: 10 attempts per IP per minute via the
    in-process token bucket. Eleventh attempt within the window
    returns 429 with Retry-After: 60. Successful logins still count
    against the bucket (so a wrong-password storm followed by a
    correct one still counts toward the cap) — the alternative
    (refund on success) leaks "this username/password was correct"
    via timing.
    """
    if not await login_limiter.check_and_consume(client_key(request)):
        logger.info(
            "login_rate_limited ip=%s username=%s",
            client_key(request),
            username,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again in a minute.",
            headers={"Retry-After": "60"},
        )
    settings = get_settings()
    htmx_or_form = _is_htmx_or_form(request)

    # Pull credentials from form OR JSON body.
    if username is None or password is None:
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            try:
                parsed = LoginRequest(**body)
                username = parsed.username
                password = parsed.password
            except Exception:
                username = password = None

    if not username or not password:
        if htmx_or_form:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Username and password are required."},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username and password are required",
        )

    user = await _authenticate(db, username, password)
    if user is None:
        logger.info("login_failed username=%s", username)
        if htmx_or_form:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Invalid username or password."},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    expires = timedelta(minutes=settings.jwt_expire_minutes)
    token = create_access_token(
        subject=str(user.id),
        role=user.role,
        expires_delta=expires,
    )
    max_age = int(expires.total_seconds())

    logger.info("login_success user_id=%s role=%s", user.id, user.role)

    if _is_htmx(request):
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.headers["HX-Redirect"] = "/"
        _set_cookie(response, token, max_age)
        return response

    if _is_form_post(request):
        # Plain browser form submit (no HTMX) — use 303 to switch to GET on /
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        _set_cookie(response, token, max_age)
        return response

    payload = TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=max_age,
    )
    response = JSONResponse(content=payload.model_dump())
    _set_cookie(response, token, max_age)
    return response


@router.post("/logout")
async def logout(request: Request):
    """Clear the auth cookie and tell HTMX clients to redirect to login."""
    response = Response(status_code=status.HTTP_200_OK)
    response.headers["HX-Redirect"] = "/auth/login"
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
    )
    return response


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    """Return the currently-authenticated user."""
    return UserOut.model_validate(user)


@router.post("/ws-token")
async def issue_ws_token_endpoint(
    request: Request,
    user: User = Depends(get_current_user),
):
    """Mint a short-lived JWT for WebSocket auth (Phase 10).

    Required by ``/ws/runs/{id}`` — the WS handler verifies this token
    before accepting the connection. The cookie is no longer enough on
    its own: a cross-origin page could trigger a WS upgrade and the
    browser would attach the cookie, and CSRF middleware doesn't run
    on WS upgrades.

    Bound to the authenticated user's id; expires in 30 seconds. The
    browser calls this immediately before opening the WS so the token
    has its full lifetime ahead of it.

    Rate-limited (60/min/IP) — the run-detail page calls this on every
    WS reconnect, which can be frequent for an operator triaging a
    flaky stack.
    """
    if not await ws_token_limiter.check_and_consume(client_key(request)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many ws-token requests. Try again in a minute.",
            headers={"Retry-After": "30"},
        )
    token = issue_ws_token(str(user.id))
    return {"token": token, "expires_in": WS_TOKEN_TTL_SECONDS}
