"""Admin-only HTML routes.

Every page renders the shared `partials/page_shell.html` shell with a
`current_tab` value matching the route URL so the sidebar/tab strip
highlights the active section. Most pages are intentional placeholders
until their phase is implemented (see README/plan).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.models.users import User
from app.routers.dashboard import _ctx, templates
from app.security.deps import require_admin

router = APIRouter(prefix="/admin", tags=["admin-ui"], include_in_schema=False)


def _render(request: Request, user: User, template_name: str, current_tab: str):
    return templates.TemplateResponse(
        template_name, _ctx(request, user, current_tab=current_tab)
    )


@router.get("/dashboard")
async def admin_dashboard(
    request: Request,
    user: User = Depends(require_admin),
):
    return _render(request, user, "admin/dashboard.html", "/admin/dashboard")


@router.get("/servers")
async def admin_servers(
    request: Request,
    user: User = Depends(require_admin),
):
    return _render(request, user, "admin/servers.html", "/admin/servers")


@router.get("/agents")
async def admin_agents(
    request: Request,
    user: User = Depends(require_admin),
):
    return _render(request, user, "admin/agents.html", "/admin/agents")


@router.get("/customers")
async def admin_customers(
    request: Request,
    user: User = Depends(require_admin),
):
    return _render(request, user, "admin/customers.html", "/admin/customers")


@router.get("/stacks")
async def admin_stacks(
    request: Request,
    user: User = Depends(require_admin),
):
    return _render(request, user, "admin/stacks.html", "/admin/stacks")


@router.get("/settings")
async def admin_settings(
    request: Request,
    user: User = Depends(require_admin),
):
    return _render(request, user, "admin/settings.html", "/admin/settings")


@router.get("/audit")
async def admin_audit(
    request: Request,
    user: User = Depends(require_admin),
):
    return _render(request, user, "admin/audit.html", "/admin/audit")
