from __future__ import annotations

from pathlib import Path

from fastapi import Request

from app.models.users import User
from app.security.csrf import get_csrf_token_from_request
from fastapi.templating import Jinja2Templates

# Shared Jinja loader + context helper used by admin.py and agent.py.
# Kept in this module so per-role routers can import it without each one
# instantiating its own templates engine.

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _ctx(request: Request, user: User, *, current_tab: str = "") -> dict:
    """Build the standard template context.

    `current_tab` is the URL of the active tab so the shared `page_shell.html`
    partial can highlight the matching sidebar/tab link.

    ``csrf_token_value`` is the value of the ``csrf_token`` cookie set by
    :class:`app.security.csrf.CSRFMiddleware`. ``base.html`` renders it
    into a ``<meta name="csrf-token">`` tag (read by ``app.js`` for the
    HTMX path), and every ``<form method="post">`` template includes
    ``partials/csrf.html`` which renders the same value as a hidden
    ``csrf_token`` input.
    """
    return {
        "request": request,
        "current_user": user,
        "app_name": "Seller-Market Management",
        "app_version": "0.1.0",
        "flashes": [],
        "current_tab": current_tab,
        "csrf_token_value": get_csrf_token_from_request(request) or "",
    }
