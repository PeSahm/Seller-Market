"""Admin-only HTML routes.

Every page renders the shared `partials/page_shell.html` shell with a
`current_tab` value matching the route URL so the sidebar/tab strip
highlights the active section. Most pages are intentional placeholders
until their phase is implemented (see README/plan).

Server CRUD (Phase 2) lives in this module too. The business logic for
those routes is in :mod:`app.services.servers`; the handlers here only
deal with form parsing, validation, and template selection.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.servers import ServerClockSkewSample
from app.models.users import User
from app.routers.dashboard import _ctx, templates
from app.schemas.server import ServerCreatePassword, ServerCreatePubkey
from app.security.deps import require_admin
from app.services import servers as services_servers

router = APIRouter(prefix="/admin", tags=["admin-ui"], include_in_schema=False)


def _render(request: Request, user: User, template_name: str, current_tab: str):
    return templates.TemplateResponse(
        template_name, _ctx(request, user, current_tab=current_tab)
    )


@router.get("/dashboard")
async def admin_dashboard(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin overview. Cards backed by live data when their phase has shipped."""
    server_rows = await services_servers.list_servers(db)
    server_summary = {
        "total": len(server_rows),
        "online": sum(1 for s in server_rows if s.status == "online"),
        "offline": sum(1 for s in server_rows if s.status == "offline"),
        "unknown": sum(1 for s in server_rows if s.status == "unknown"),
        "unpinned": sum(1 for s in server_rows if not s.host_key_pin),
    }
    ctx = _ctx(request, user, current_tab="/admin/dashboard")
    ctx["server_summary"] = server_summary
    return templates.TemplateResponse("admin/dashboard.html", ctx)


# ---------------------------------------------------------------------------
# Servers (Phase 2)
# ---------------------------------------------------------------------------


@router.get("/servers")
async def admin_servers(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all configured servers."""
    server_rows = await services_servers.list_servers(db)
    ctx = _ctx(request, user, current_tab="/admin/servers")
    ctx["servers"] = server_rows
    return templates.TemplateResponse("admin/servers.html", ctx)


@router.get("/servers/new")
async def admin_server_new(
    request: Request,
    user: User = Depends(require_admin),
):
    """Render the "add server" form."""
    ctx = _ctx(request, user, current_tab="/admin/servers")
    ctx["form_error"] = None
    ctx["form_values"] = {}
    return templates.TemplateResponse("admin/server_form.html", ctx)


@router.post("/servers")
async def admin_server_create(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    host: str = Form(...),
    ssh_port: int = Form(22),
    ssh_user: str = Form(...),
    base_dir: str = Form("/root/seller-market/agents"),
    ssh_auth: str = Form(...),
    password: Optional[str] = Form(None),
    private_key: Optional[str] = Form(None),
):
    """Create a new server row + persist its SSH credential.

    Validation happens in two places: pydantic checks shape (port range,
    base_dir is absolute POSIX, etc.) and this handler checks the auth /
    secret combination is consistent.

    On validation failure we re-render the form with an error message and
    the user's previous values (minus the secret) so they don't have to
    retype everything. On success we redirect to the detail page; HTMX
    callers see the redirect via ``HX-Redirect``, plain form submitters
    follow a 303.
    """
    common = {
        "name": name,
        "host": host,
        "ssh_port": ssh_port,
        "ssh_user": ssh_user,
        "base_dir": base_dir,
    }

    try:
        if ssh_auth == "password":
            if not password:
                raise ValueError("password is required for password auth")
            payload = ServerCreatePassword(password=password, **common)
        elif ssh_auth == "pubkey":
            if not private_key:
                raise ValueError("private key is required for pubkey auth")
            payload = ServerCreatePubkey(private_key=private_key, **common)
        else:
            raise ValueError(f"unknown ssh_auth: {ssh_auth!r}")
    except (ValidationError, ValueError) as exc:
        ctx = _ctx(request, user, current_tab="/admin/servers")
        ctx["form_error"] = str(exc)
        # Note: we intentionally drop ``password`` and ``private_key`` from the
        # re-rendered form — secrets MUST NOT round-trip through the HTML.
        ctx["form_values"] = {**common, "ssh_auth": ssh_auth}
        return templates.TemplateResponse(
            "admin/server_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    server = await services_servers.create_server(db, payload, actor_id=user.id)

    redirect_to = f"/admin/servers/{server.id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


@router.get("/servers/{server_id}")
async def admin_server_detail(
    server_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render the per-server detail page with the last 10 clock-skew samples."""
    server = await services_servers.get_server(db, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")

    # Last 10 samples, newest first — drives the small chart/table the
    # templates agent owns.
    stmt = (
        select(ServerClockSkewSample)
        .where(ServerClockSkewSample.server_id == server_id)
        .order_by(desc(ServerClockSkewSample.sampled_at))
        .limit(10)
    )
    skew_result = await db.execute(stmt)
    skew_samples = list(skew_result.scalars().all())

    ctx = _ctx(request, user, current_tab="/admin/servers")
    ctx["server"] = server
    ctx["skew_samples"] = skew_samples
    return templates.TemplateResponse("admin/server_detail.html", ctx)


@router.post("/servers/{server_id}/test-connection")
async def admin_server_test_connection(
    server_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Run an SSH probe and return an HTMX partial summarising the result.

    The partial template (``admin/partials/test_connection_result.html``) is
    owned by the templates agent — we just pass it the
    :class:`~app.schemas.server.TestConnectionResult` and let it pick the
    right badge / colour scheme.
    """
    try:
        probe = await services_servers.test_connection(
            db, server_id, actor_id=user.id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    ctx = _ctx(request, user, current_tab="/admin/servers")
    ctx["result"] = probe
    return templates.TemplateResponse(
        "admin/partials/test_connection_result.html", ctx
    )


@router.post("/servers/{server_id}/delete")
async def admin_server_delete(
    server_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete a server (Phase 2 scope — no soft delete yet).

    Removes the on-disk key file if pubkey auth, deletes the row, writes
    the audit entry. HTMX gets an ``HX-Redirect``; plain forms get a 303.
    """
    try:
        await services_servers.soft_delete_server(
            db, server_id, actor_id=user.id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    redirect_to = "/admin/servers"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


# ---------------------------------------------------------------------------
# Remaining placeholder pages — each implemented in its own phase.
# ---------------------------------------------------------------------------


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


@router.get("/act-as")
async def admin_act_as(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List agents an admin can view-as. Real impersonation lands in Phase 4."""
    stmt = (
        select(User)
        .where(User.role == "agent", User.deleted_at.is_(None))
        .order_by(User.username)
    )
    result = await db.execute(stmt)
    agents = result.scalars().all()
    ctx = _ctx(request, user, current_tab="/admin/act-as")
    ctx["agents"] = agents
    return templates.TemplateResponse("admin/act_as.html", ctx)


# Silence unused-import lint for HTMLResponse — kept for forward use by the
# test-connection partial endpoint if we ever need to return a raw fragment
# instead of a TemplateResponse.
_ = HTMLResponse
