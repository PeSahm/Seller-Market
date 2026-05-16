from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.users import User
from app.schemas.auth import LoginRequest, TokenResponse, UserOut
from app.security.auth import create_access_token, verify_password
from app.security.deps import COOKIE_NAME, get_current_user
from app.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _is_htmx_or_form(request: Request) -> bool:
    """Detect HTMX requests or form-encoded posts (vs JSON API clients)."""
    if request.headers.get("HX-Request", "").lower() == "true":
        return True
    content_type = request.headers.get("content-type", "")
    return content_type.startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    )


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
    """
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

    if htmx_or_form:
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.headers["HX-Redirect"] = "/"
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
