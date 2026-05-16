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

import logging
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
from app.schemas.agent import AgentCreate
from app.schemas.server import ServerCreatePassword, ServerCreatePubkey
from app.schemas.settings_page import SettingsUpdate
from app.security.deps import require_admin
from app.services import agents as services_agents
from app.services import servers as services_servers
from app.services import settings_store
from app.services import stacks as services_stacks
from app.services.ssh.exceptions import SSHError

logger = logging.getLogger(__name__)

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
    stack_rows = await services_stacks.list_stacks(db)
    stack_summary = {
        "total": len(stack_rows),
        "up": sum(1 for s in stack_rows if s.status == "up"),
        "down": sum(1 for s in stack_rows if s.status == "down"),
        "provisioning": sum(1 for s in stack_rows if s.status == "provisioning"),
    }
    ctx = _ctx(request, user, current_tab="/admin/dashboard")
    ctx["server_summary"] = server_summary
    ctx["stack_summary"] = stack_summary
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
        if isinstance(exc, ValidationError):
            ctx["form_error"] = "Invalid input. Please review the form fields and try again."
        else:
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
    ctx["clock_skew_samples"] = skew_samples
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
# Agents (Phase 3)
# ---------------------------------------------------------------------------


@router.get("/agents/new")
async def admin_agent_new(
    request: Request,
    user: User = Depends(require_admin),
):
    """Render the "add agent" form with empty values."""
    ctx = _ctx(request, user, current_tab="/admin/agents")
    ctx["form_error"] = None
    ctx["form_values"] = {}
    return templates.TemplateResponse("admin/agent_form.html", ctx)


@router.post("/agents")
async def admin_agent_create(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    username: str = Form(...),
    password: str = Form(...),
    telegram_user_id: Optional[str] = Form(None),
):
    """Create a new agent (User row with role='agent').

    Validation happens in two layers: pydantic checks shape (length bounds),
    and the service layer enforces username uniqueness. On either failure we
    re-render the form with an error message and the user's previous values —
    EXCEPT the password, which MUST NOT round-trip through the HTML.

    On success: redirect to the detail page. HTMX callers see the redirect
    via ``HX-Redirect``; plain form submitters follow a 303.
    """
    # Normalize the telegram_user_id: an empty <input> arrives as "" which
    # we want to treat as "not set" rather than store a literal empty string.
    tg_id = telegram_user_id.strip() if telegram_user_id else None
    if tg_id == "":
        tg_id = None

    sticky = {"username": username, "telegram_user_id": tg_id or ""}

    try:
        payload = AgentCreate(
            username=username,
            password=password,
            telegram_user_id=tg_id,
        )
    except ValidationError:
        ctx = _ctx(request, user, current_tab="/admin/agents")
        ctx["form_error"] = (
            "Invalid input. Username must be 1–255 chars, password ≥ 8 chars."
        )
        # Note: password is intentionally dropped from form_values — secrets
        # MUST NOT round-trip through the HTML.
        ctx["form_values"] = sticky
        return templates.TemplateResponse(
            "admin/agent_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        agent = await services_agents.create_agent(
            db, payload, actor_id=user.id
        )
    except ValueError as exc:
        ctx = _ctx(request, user, current_tab="/admin/agents")
        ctx["form_error"] = str(exc)
        ctx["form_values"] = sticky
        return templates.TemplateResponse(
            "admin/agent_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    redirect_to = f"/admin/agents/{agent.id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


@router.get("/agents")
async def admin_agents(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all active agents."""
    agents = await services_agents.list_agents(db)
    ctx = _ctx(request, user, current_tab="/admin/agents")
    ctx["agents"] = agents
    return templates.TemplateResponse("admin/agents.html", ctx)


@router.get("/agents/{agent_id}")
async def admin_agent_detail(
    agent_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render the per-agent detail page."""
    agent = await services_agents.get_agent(db, agent_id)
    if agent is None or agent.role != "agent":
        raise HTTPException(status_code=404, detail="agent not found")
    ctx = _ctx(request, user, current_tab="/admin/agents")
    ctx["agent"] = agent
    return templates.TemplateResponse("admin/agent_detail.html", ctx)


@router.post("/agents/{agent_id}/delete")
async def admin_agent_delete(
    agent_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete an agent (sets ``deleted_at = now()``).

    The row stays in the DB so audit history and any orphan customer / stack
    rows keep their FK target. Phase 4 / Phase 9 will own the actual cleanup.
    """
    try:
        await services_agents.soft_delete_agent(
            db, agent_id, actor_id=user.id
        )
    except ValueError as exc:
        # Service refuses to soft-delete a non-agent (defense in depth) —
        # surface that as a 400 rather than a 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    redirect_to = "/admin/agents"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


# ---------------------------------------------------------------------------
# Remaining placeholder pages — each implemented in its own phase.
# ---------------------------------------------------------------------------


@router.get("/customers")
async def admin_customers(
    request: Request,
    user: User = Depends(require_admin),
):
    return _render(request, user, "admin/customers.html", "/admin/customers")


# ---------------------------------------------------------------------------
# Stacks (Phase 3)
# ---------------------------------------------------------------------------


@router.get("/stacks")
async def admin_stacks(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all agent stacks across all servers.

    Pre-loads server/agent lookup dicts so the template can resolve FK ids to
    human labels without lazy-loading per row. Soft-deleted agents are
    included on purpose: a stack outlives its owner until Phase 9 cleanup, and
    we still want to show *which* (now-deleted) agent it belongs to.
    """
    stacks = await services_stacks.list_stacks(db)
    servers = {s.id: s for s in await services_servers.list_servers(db)}
    agents = {a.id: a for a in await services_agents.list_agents(db, include_deleted=True)}
    ctx = _ctx(request, user, current_tab="/admin/stacks")
    ctx["stacks"] = stacks
    ctx["servers_by_id"] = servers
    ctx["agents_by_id"] = agents
    return templates.TemplateResponse("admin/stacks.html", ctx)


@router.get("/stacks/new")
async def admin_stack_new(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Form to provision a new stack: pick agent + server."""
    servers = await services_servers.list_servers(db)
    agents = await services_agents.list_agents(db)
    ctx = _ctx(request, user, current_tab="/admin/stacks")
    ctx["servers"] = servers
    ctx["agents"] = agents
    ctx["form_error"] = None
    return templates.TemplateResponse("admin/stack_new.html", ctx)


@router.post("/stacks")
async def admin_stack_create(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    server_id: UUID = Form(...),
    agent_id: UUID = Form(...),
):
    """Find-or-create the (server, agent) stack row, then provision it.

    If a stack for this pair already exists, ``find_or_create_stack`` returns
    it; otherwise it inserts a new row in ``provisioning`` state. We then call
    ``provision_stack`` to render the per-agent files on the remote host and
    bring the compose project up. If another compose op is already in flight,
    the service raises ``RuntimeError`` and we surface that as a 400.
    """
    server = await services_servers.get_server(db, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    agent = await services_agents.get_agent(db, agent_id)
    if agent is None or agent.role != "agent" or agent.deleted_at is not None:
        # Treat soft-deleted as "not found" to keep the surface uniform and
        # prevent enumeration of deleted records.
        raise HTTPException(status_code=404, detail="agent not found")

    stack = await services_stacks.find_or_create_stack(
        db, server, agent_id, actor_id=user.id
    )
    try:
        await services_stacks.provision_stack(db, stack.id, actor_id=user.id)
    except RuntimeError as exc:
        # Another compose op already in flight for this stack. Phase 3 just
        # surfaces this as a flat 400 — Phase >3 will get a nicer retry UX.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SSHError:
        # SSH/SFTP failure mid-provision. The service has already persisted
        # status='down' on the row; redirect to the detail page so the admin
        # can see the failure context + retry.
        pass

    redirect_to = f"/admin/stacks/{stack.id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


@router.get("/stacks/{stack_id}")
async def admin_stack_detail(
    stack_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render the per-stack detail page (identity + rendered file preview)."""
    stack = await services_stacks.get_stack(db, stack_id)
    if stack is None:
        raise HTTPException(status_code=404, detail="stack not found")
    server = await services_servers.get_server(db, stack.server_id)
    agent = await services_agents.get_agent(db, stack.agent_id)

    files: dict[str, str] = {}
    try:
        files = await services_stacks.stack_files_preview(db, stack_id)
    except SSHError as exc:
        # Server unreachable, key rejected, file missing, etc. — expected when
        # the stack hasn't been provisioned yet or the server is offline.
        # Log + degrade so the admin can still see the page and take action.
        logger.info("stack_files_preview failed for stack=%s: %s", stack_id, exc)

    ctx = _ctx(request, user, current_tab="/admin/stacks")
    ctx["stack"] = stack
    ctx["server"] = server
    ctx["agent"] = agent
    ctx["files"] = files
    return templates.TemplateResponse("admin/stack_detail.html", ctx)


@router.post("/stacks/{stack_id}/redeploy")
async def admin_stack_redeploy(
    stack_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Re-render files and ``docker compose up -d`` for an existing stack.

    Returns the action partial so HTMX can swap it into ``#stack-action-result``
    on the detail page.
    """
    try:
        result = await services_stacks.redeploy_stack(
            db, stack_id, actor_id=user.id
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx = _ctx(request, user, current_tab="/admin/stacks")
    ctx["result"] = result
    return templates.TemplateResponse(
        "admin/partials/stack_action_result.html", ctx
    )


@router.post("/stacks/{stack_id}/deprovision")
async def admin_stack_deprovision(
    stack_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Tear down a stack: ``docker compose down`` + remove the stack_dir.

    On success we redirect back to the list view. On failure we render the
    action partial inline so the admin can see *why* it failed (typical cause:
    server is unreachable so we can't SSH in to clean up).
    """
    try:
        result = await services_stacks.deprovision_stack(
            db, stack_id, actor_id=user.id
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if result.ok:
        redirect_to = "/admin/stacks"
        if request.headers.get("HX-Request"):
            return Response(status_code=204, headers={"HX-Redirect": redirect_to})
        return Response(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": redirect_to},
        )

    # Failed deprovision — render the action partial so admin can see why.
    ctx = _ctx(request, user, current_tab="/admin/stacks")
    ctx["result"] = result
    return templates.TemplateResponse(
        "admin/partials/stack_action_result.html", ctx
    )


@router.get("/settings")
async def admin_settings(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render the admin Settings page with current values (DB row or default)."""
    values = await settings_store.get_all_settings(db)
    ctx = _ctx(request, user, current_tab="/admin/settings")
    ctx["settings_values"] = values
    ctx["form_error"] = None
    return templates.TemplateResponse("admin/settings.html", ctx)


@router.post("/settings")
async def admin_settings_save(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    ocr_service_url: str = Form(...),
    agent_image_tag: str = Form(...),
):
    """Persist the admin Settings form.

    Validation lives in :class:`~app.schemas.settings_page.SettingsUpdate`.
    On failure we re-render the page with the user's typed values and a
    form-level error (no field-by-field errors yet — the form only has two
    fields, so a single banner is fine).

    On success we 303-redirect back to the page; HTMX callers see the same
    redirect via ``HX-Redirect`` so their URL bar updates.
    """
    try:
        validated = SettingsUpdate(
            ocr_service_url=ocr_service_url,
            agent_image_tag=agent_image_tag,
        )
    except (ValidationError, ValueError) as exc:
        ctx = _ctx(request, user, current_tab="/admin/settings")
        # Show what the user typed, not the stored values — they need to be
        # able to correct their input.
        ctx["settings_values"] = {
            "ocr_service_url": ocr_service_url,
            "agent_image_tag": agent_image_tag,
        }
        if isinstance(exc, ValidationError):
            ctx["form_error"] = (
                "Invalid input. Please review the form fields and try again."
            )
        else:
            ctx["form_error"] = str(exc)
        return templates.TemplateResponse(
            "admin/settings.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    await settings_store.set_setting(
        db, "ocr_service_url", validated.ocr_service_url, updated_by=user.id
    )
    await settings_store.set_setting(
        db, "agent_image_tag", validated.agent_image_tag, updated_by=user.id
    )
    await db.commit()

    if request.headers.get("HX-Request"):
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"HX-Redirect": "/admin/settings"},
        )
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": "/admin/settings"},
    )


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
