from __future__ import annotations

from pathlib import Path

from fastapi import Request

from app.models.users import User
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
    """
    return {
        "request": request,
        "current_user": user,
        "app_name": "Seller-Market Management",
        "app_version": "0.1.0",
        "flashes": [],
        "current_tab": current_tab,
    }
