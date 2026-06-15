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

# `symbol_label(isin, symbol=None, title=None)` — resolves an ISIN to a friendly
# symbol/name (warm in-memory cache) and renders "symbol + muted ISIN". Registered
# once here so every template rendered through this shared engine (admin.py,
# agent.py, brokers_admin.py and all their partials) can call it. Sync + graceful
# (falls back to the bare ISIN); the cache is warmed at startup and refreshed via
# `ensure_instruments` at the top of the ISIN-grid routes.
from app.services.instruments import render_symbol_label, symbol_text  # noqa: E402

templates.env.globals["symbol_label"] = render_symbol_label
# `symbol_text(isin, symbol=None, title=None)` — the same resolution as
# `symbol_label` but returns a plain label string (no HTML) for inline/header
# contexts where the block "symbol + muted ISIN" layout doesn't fit.
templates.env.globals["symbol_text"] = symbol_text


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
