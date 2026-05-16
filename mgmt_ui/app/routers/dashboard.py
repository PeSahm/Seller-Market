from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates

from app.models.users import User
from app.security.deps import get_current_user, require_admin

router = APIRouter()

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _ctx(request: Request, user: User) -> dict:
    return {
        "request": request,
        "current_user": user,
        "app_name": "Seller-Market Management",
        "app_version": "0.1.0",
        "flashes": [],
    }


@router.get("/admin/dashboard", include_in_schema=False)
async def admin_dashboard(
    request: Request,
    user: User = Depends(require_admin),
):
    return templates.TemplateResponse("admin/dashboard.html", _ctx(request, user))


@router.get("/agent/dashboard", include_in_schema=False)
async def agent_dashboard(
    request: Request,
    user: User = Depends(get_current_user),
):
    if user.role not in ("agent", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return templates.TemplateResponse("agent/dashboard.html", _ctx(request, user))
