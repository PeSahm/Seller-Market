from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.models.users import User
from app.routers import auth as auth_router
from app.routers import dashboard as dashboard_router
from app.routers import health as health_router
from app.security.deps import get_current_user
from app.settings import get_settings

logger = logging.getLogger(__name__)


def _wants_html(request: Request) -> bool:
    """Heuristic: is this a browser navigation (HTML) vs an API client?"""
    if request.headers.get("HX-Request", "").lower() == "true":
        return True
    accept = request.headers.get("accept", "")
    return "text/html" in accept.lower()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0")

    # Static files (agent #4 owns the directory contents).
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(static_dir)),
            name="static",
        )
    else:
        logger.warning("static directory %s does not exist; skipping mount", static_dir)

    # Routers
    app.include_router(health_router.router)
    app.include_router(auth_router.router)
    app.include_router(dashboard_router.router)

    # Root: route to admin or agent dashboard based on role.
    @app.get("/", include_in_schema=False)
    async def root(user: User = Depends(get_current_user)) -> RedirectResponse:
        if user.role == "admin":
            return RedirectResponse(url="/admin/dashboard")
        return RedirectResponse(url="/agent/dashboard")

    # Unified 401 handling: browsers get redirected to login, APIs get JSON.
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse | RedirectResponse:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED and _wants_html(request):
            # Skip redirect for the login page itself to avoid loops.
            if not request.url.path.startswith("/auth/"):
                response = RedirectResponse(
                    url="/auth/login",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
                if request.headers.get("HX-Request", "").lower() == "true":
                    response.headers["HX-Redirect"] = "/auth/login"
                return response
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None) or {},
        )

    @app.on_event("startup")
    async def _log_startup() -> None:
        # Do NOT reveal key material — only report whether parts are configured.
        part1_set = bool(settings.fernet_key_part1.get_secret_value())
        part2_present = os.path.exists(settings.fernet_key_part2_path)
        logger.info(
            "startup app=%s env=%s fernet_part1=%s fernet_part2=%s (path=%s)",
            settings.app_name,
            settings.environment,
            "set" if part1_set else "MISSING",
            "found" if part2_present else "missing(dev fallback)",
            settings.fernet_key_part2_path,
        )

    return app


app = create_app()
