"""Agent (and admin-as-agent) HTML routes.

Per the plan, admins may also reach `/agent/*` pages so they can act as
any agent. We therefore use `get_current_user` + an inline role check
rather than a strict `require_agent`-only guard (which would already
allow admins, but we keep the explicit check for clarity).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.models.users import User
from app.routers.dashboard import _ctx, templates
from app.security.deps import get_current_user

router = APIRouter(prefix="/agent", tags=["agent-ui"], include_in_schema=False)


def _require_agent_or_admin(user: User) -> None:
    if user.role not in ("agent", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


def _render(request: Request, user: User, template_name: str, current_tab: str):
    return templates.TemplateResponse(
        template_name, _ctx(request, user, current_tab=current_tab)
    )


@router.get("/dashboard")
async def agent_dashboard(
    request: Request,
    user: User = Depends(get_current_user),
):
    _require_agent_or_admin(user)
    return _render(request, user, "agent/dashboard.html", "/agent/dashboard")


@router.get("/customers")
async def agent_customers(
    request: Request,
    user: User = Depends(get_current_user),
):
    _require_agent_or_admin(user)
    return _render(request, user, "agent/customers.html", "/agent/customers")


@router.get("/stacks")
async def agent_stacks(
    request: Request,
    user: User = Depends(get_current_user),
):
    _require_agent_or_admin(user)
    return _render(request, user, "agent/stacks.html", "/agent/stacks")


@router.get("/history")
async def agent_history(
    request: Request,
    user: User = Depends(get_current_user),
):
    _require_agent_or_admin(user)
    return _render(request, user, "agent/history.html", "/agent/history")


@router.get("/logs")
async def agent_logs(
    request: Request,
    user: User = Depends(get_current_user),
):
    _require_agent_or_admin(user)
    return _render(request, user, "agent/logs.html", "/agent/logs")
