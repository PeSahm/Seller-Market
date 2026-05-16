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

import difflib
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
from app.schemas.customer import CustomerCreate, CustomerUpdate
from app.schemas.server import ServerCreatePassword, ServerCreatePubkey
from app.schemas.settings_page import SettingsUpdate
from app.security.deps import require_admin
from app.services import agents as services_agents
from app.services import customers as services_customers
from app.services import distribution as services_distribution
from app.services import servers as services_servers
from app.services import settings_store
from app.services import stacks as services_stacks
from app.services.customers import OptimisticLockError
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

    # Phase 4: pending-customer inbox summary. Counts customers an agent
    # has declared but whose ``assignment_status`` is still ``pending`` —
    # i.e. they're waiting on an admin to place them on a server. The
    # by_agent breakdown lets the dashboard card show "5 pending across
    # 3 agents" without a second query.
    pending = await services_distribution.pending_customers(db)
    by_agent: dict[str, int] = {}
    for c in pending:
        key = str(c.agent_id)
        by_agent[key] = by_agent.get(key, 0) + 1
    pending_summary = {"total": len(pending), "by_agent": by_agent}

    # Phase 4: lightweight customer roll-up for the dashboard. We
    # deliberately use ``include_disabled=False`` here so the card mirrors
    # what an agent sees in their own view (soft-deleted customers don't
    # count toward "active").
    all_customers = await services_customers.list_customers(db, include_disabled=False)
    customer_summary = {
        "total": len(all_customers),
        "active": sum(1 for c in all_customers if c.assignment_status == "active"),
        "assigned": sum(1 for c in all_customers if c.assignment_status == "assigned"),
        "pending": sum(1 for c in all_customers if c.assignment_status == "pending"),
    }

    ctx = _ctx(request, user, current_tab="/admin/dashboard")
    ctx["server_summary"] = server_summary
    ctx["stack_summary"] = stack_summary
    ctx["pending_summary"] = pending_summary
    ctx["customer_summary"] = customer_summary
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
    """Render the per-agent detail page with live customer + stack data."""
    agent = await services_agents.get_agent(db, agent_id)
    if agent is None or agent.role != "agent":
        raise HTTPException(status_code=404, detail="agent not found")
    agent_customers = await services_customers.list_customers(
        db, agent_id=agent_id, include_disabled=False,
    )
    all_stacks = await services_stacks.list_stacks(db)
    agent_stacks_list = [s for s in all_stacks if s.agent_id == agent_id]
    servers_by_id = {
        s.id: s for s in await services_servers.list_servers(db)
    }
    ctx = _ctx(request, user, current_tab="/admin/agents")
    ctx["agent"] = agent
    ctx["agent_customers"] = agent_customers
    ctx["agent_stacks"] = agent_stacks_list
    ctx["servers_by_id"] = servers_by_id
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
# Customers (Phase 4)
# ---------------------------------------------------------------------------
#
# Customer CRUD + distribution (assign / unassign / move) all live here.
# Each route is admin-only via ``Depends(require_admin)``. The actual
# business logic — Fernet password encryption, section-name generation,
# stack lookup, audit logging — sits in :mod:`app.services.customers` and
# :mod:`app.services.distribution`; the handlers here just translate forms,
# pass them down, and pick the right template.
#
# The "assign" page additionally renders a server-side diff of the
# proposed ``config.ini`` change against the remote file. The diff
# computation is done HERE (with :mod:`difflib`) so the template stays
# pure-presentational — the partial just iterates the pre-formatted lines
# and styles each one by its leading character.


def _flash_redirect(request: Request, location: str) -> Response:
    """Common 303 / HX-Redirect pattern used across the customer routes.

    HTMX callers receive a 204 + ``HX-Redirect`` header so the browser
    transitions cleanly without a hard reload; plain form submitters get
    a regular 303 See Other and the browser follows it.
    """
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": location})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": location},
    )


@router.get("/customers")
async def admin_customers(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    broker: Optional[str] = None,
):
    """List all customers with optional filter chips.

    The filter chips (agent / status / broker) round-trip through query
    parameters so a filtered view is bookmarkable and link-shareable. We
    include soft-deleted agents in the lookup dict because a customer can
    outlive its agent (Phase 9 cleanup); we still want to render *which*
    deleted agent owned them.

    All three filter args accept the empty string (sent by an unselected
    ``<select><option value="">…</option>`` from the filter bar) and treat
    it as "no filter". ``agent_id`` is typed as ``str`` rather than ``UUID``
    here so an empty value doesn't fail pydantic parsing and 422 the page
    into JSON; we parse it manually below.
    """
    agent_uuid: Optional[UUID] = None
    if agent_id:
        try:
            agent_uuid = UUID(agent_id)
        except ValueError:
            # Bad UUID in the query string — treat as "no filter" rather
            # than 422 the page.
            agent_uuid = None
    status = status or None
    broker = broker or None
    customers = await services_customers.list_customers(
        db,
        agent_id=agent_uuid,
        status=status,
        broker=broker,
        include_disabled=False,
    )
    agents = {
        a.id: a
        for a in await services_agents.list_agents(db, include_deleted=True)
    }
    servers = {s.id: s for s in await services_servers.list_servers(db)}
    ctx = _ctx(request, user, current_tab="/admin/customers")
    ctx["customers"] = customers
    ctx["agents_by_id"] = agents
    ctx["servers_by_id"] = servers
    ctx["filter_agent_id"] = agent_id
    ctx["filter_status"] = status
    ctx["filter_broker"] = broker
    ctx["all_agents"] = list(agents.values())
    return templates.TemplateResponse("admin/customers.html", ctx)


@router.get("/customers/pending")
async def admin_customers_pending(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """The admin inbox of customers awaiting server assignment.

    Filters down to ``assignment_status='pending'`` only. Each row gets
    an inline "Assign to <select>" form so an admin can clear the inbox
    one customer at a time without leaving the page.
    """
    customers = await services_distribution.pending_customers(db)
    agents = {
        a.id: a
        for a in await services_agents.list_agents(db, include_deleted=True)
    }
    servers = await services_servers.list_servers(db)
    ctx = _ctx(request, user, current_tab="/admin/customers")
    ctx["customers"] = customers
    ctx["agents_by_id"] = agents
    ctx["servers"] = servers
    return templates.TemplateResponse("admin/customers_pending.html", ctx)


@router.get("/customers/new")
async def admin_customer_new(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render the "add customer" form (admin-side, picks the owning agent)."""
    agents = await services_agents.list_agents(db)
    ctx = _ctx(request, user, current_tab="/admin/customers")
    ctx["agents"] = agents
    ctx["form_error"] = None
    ctx["form_values"] = {}
    ctx["mode"] = "create"
    return templates.TemplateResponse("admin/customer_form.html", ctx)


@router.post("/customers")
async def admin_customer_create(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    agent_id: UUID = Form(...),
    display_name: str = Form(...),
    broker: str = Form(...),
    isin: str = Form(...),
    side: int = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    comment: Optional[str] = Form(None),
):
    """Create a new customer owned by ``agent_id``.

    Validation happens in two layers (same shape as
    :func:`admin_server_create` and :func:`admin_agent_create`):
    pydantic checks shape and the service layer enforces the composite
    UNIQUE on ``(agent, account, broker, isin, side)``. Validation errors
    re-render the form with sticky values (NEVER the password — that
    MUST NOT round-trip through the HTML).
    """
    sticky = {
        "agent_id": str(agent_id),
        "display_name": display_name,
        "broker": broker,
        "isin": isin,
        "side": str(side),
        "username": username,
        "comment": comment or "",
    }

    try:
        payload = CustomerCreate(
            display_name=display_name,
            broker=broker,  # type: ignore[arg-type]
            isin=isin,
            side=side,  # type: ignore[arg-type]
            username=username,
            password=password,
            comment=comment if comment else None,
        )
    except ValidationError:
        agents = await services_agents.list_agents(db)
        ctx = _ctx(request, user, current_tab="/admin/customers")
        ctx["agents"] = agents
        ctx["form_error"] = (
            "Invalid input. Please review the form fields and try again."
        )
        ctx["form_values"] = sticky
        ctx["mode"] = "create"
        return templates.TemplateResponse(
            "admin/customer_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        customer = await services_customers.create_customer(
            db, agent_id, payload, actor_id=user.id
        )
    except ValueError as exc:
        agents = await services_agents.list_agents(db)
        ctx = _ctx(request, user, current_tab="/admin/customers")
        ctx["agents"] = agents
        ctx["form_error"] = str(exc)
        ctx["form_values"] = sticky
        ctx["mode"] = "create"
        return templates.TemplateResponse(
            "admin/customer_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return _flash_redirect(request, f"/admin/customers/{customer.id}")


@router.get("/customers/{customer_id}")
async def admin_customer_detail(
    customer_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render the per-customer detail / identity card."""
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")

    agent = await services_agents.get_agent(db, customer.agent_id)
    server = (
        await services_servers.get_server(db, customer.server_id)
        if customer.server_id
        else None
    )
    ctx = _ctx(request, user, current_tab="/admin/customers")
    ctx["customer"] = customer
    ctx["agent"] = agent
    ctx["server"] = server
    return templates.TemplateResponse("admin/customer_detail.html", ctx)


@router.get("/customers/{customer_id}/edit")
async def admin_customer_edit_form(
    customer_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render the edit form pre-populated from the current row.

    The password is intentionally not seeded into ``form_values`` — the
    template renders an empty password input with a "leave empty to keep
    current" placeholder so the admin can rotate or leave alone. The
    row's ``version`` rides through as a hidden field for the optimistic
    lock check on POST.
    """
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")

    agent = await services_agents.get_agent(db, customer.agent_id)

    form_values = {
        "agent_id": str(customer.agent_id),
        "display_name": customer.display_name,
        "broker": customer.broker,
        "isin": customer.isin,
        "side": str(customer.side),
        "username": customer.username,
        "comment": customer.comment or "",
        "enabled": customer.enabled,
        "version": customer.version,
    }

    ctx = _ctx(request, user, current_tab="/admin/customers")
    ctx["customer"] = customer
    ctx["agent"] = agent
    ctx["form_error"] = None
    ctx["form_values"] = form_values
    ctx["mode"] = "edit"
    return templates.TemplateResponse("admin/customer_form.html", ctx)


@router.post("/customers/{customer_id}/edit")
async def admin_customer_update(
    customer_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    display_name: Optional[str] = Form(None),
    broker: Optional[str] = Form(None),
    isin: Optional[str] = Form(None),
    side: Optional[int] = Form(None),
    username: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    comment: Optional[str] = Form(None),
    enabled: Optional[str] = Form(None),  # checkbox → "on" or absent
    version: int = Form(...),
):
    """Apply an optimistic-locked update to a customer row.

    Two behaviours are worth calling out:

    * ``password=""`` is treated as "do not change" (we drop it before
      building :class:`CustomerUpdate`). Sending an empty password through
      to the service would fail pydantic's ``min_length=1`` validator
      anyway, but we'd then end up flashing a generic "Invalid input"
      message when the admin's intent was perfectly valid.
    * ``enabled`` is a checkbox: HTML submits ``"on"`` when ticked and
      omits the field entirely when not. We translate that explicitly so
      the service sees a bool rather than ``"on"`` vs ``None``.
    """
    # Build the partial update payload. Only include fields the admin
    # actually changed — ``CustomerUpdate`` uses ``exclude_unset`` semantics
    # downstream so anything omitted here stays untouched on the row.
    fields: dict = {"version": version}
    if display_name is not None and display_name != "":
        fields["display_name"] = display_name
    if broker is not None and broker != "":
        fields["broker"] = broker
    if isin is not None and isin != "":
        fields["isin"] = isin
    if side is not None:
        fields["side"] = side
    if username is not None and username != "":
        fields["username"] = username
    if password is not None and password != "":
        fields["password"] = password
    if comment is not None:
        # Empty string is a meaningful "clear comment" signal — treat it
        # as None so the column ends up NULL rather than the literal "".
        fields["comment"] = comment if comment != "" else None
    fields["enabled"] = enabled == "on"

    customer = await services_customers.get_customer(db, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")
    agent = await services_agents.get_agent(db, customer.agent_id)

    def _render_with_error(message: str, code: int):
        form_values = {
            "agent_id": str(customer.agent_id),
            "display_name": display_name if display_name is not None else customer.display_name,
            "broker": broker if broker is not None and broker != "" else customer.broker,
            "isin": isin if isin is not None and isin != "" else customer.isin,
            "side": str(side if side is not None else customer.side),
            "username": username if username is not None and username != "" else customer.username,
            "comment": comment if comment is not None else (customer.comment or ""),
            "enabled": enabled == "on",
            "version": customer.version,
        }
        ctx = _ctx(request, user, current_tab="/admin/customers")
        ctx["customer"] = customer
        ctx["agent"] = agent
        ctx["form_error"] = message
        ctx["form_values"] = form_values
        ctx["mode"] = "edit"
        return templates.TemplateResponse(
            "admin/customer_form.html",
            ctx,
            status_code=code,
        )

    try:
        payload = CustomerUpdate(**fields)
    except ValidationError:
        return _render_with_error(
            "Invalid input. Please review the form fields and try again.",
            status.HTTP_400_BAD_REQUEST,
        )

    try:
        await services_customers.update_customer(
            db, customer_id, payload, actor_id=user.id
        )
    except OptimisticLockError:
        return _render_with_error(
            "This customer was changed by someone else while you were "
            "editing. Reload the page and re-apply your changes.",
            status.HTTP_409_CONFLICT,
        )
    except ValueError as exc:
        return _render_with_error(str(exc), status.HTTP_400_BAD_REQUEST)

    return _flash_redirect(request, f"/admin/customers/{customer_id}")


@router.post("/customers/{customer_id}/delete")
async def admin_customer_delete(
    customer_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a customer.

    The service flips ``enabled=False`` and resets
    ``assignment_status='pending'`` + ``stack_id=NULL``. The render layer
    drops disabled rows from the next ``config.ini`` push, so the on-server
    bot stops trading this customer on the next render cycle without us
    having to touch the remote filesystem from this handler.
    """
    await services_customers.soft_delete_customer(
        db, customer_id, actor_id=user.id
    )
    return _flash_redirect(request, "/admin/customers")


def _compute_config_diff(before_text: str, after_text: str) -> list[str]:
    """Compute a unified diff between two ``config.ini`` snapshots.

    We render the diff server-side so the template stays free of Python
    logic. The unified-diff format prefixes each line with ``+``, ``-``,
    ``@@``, or a leading space; the template branches on that first char
    to pick the right CSS class.

    Returns an empty list if the two inputs are byte-identical — the
    template treats that as "no effective change" and skips the diff
    block entirely.
    """
    if before_text == after_text:
        return []
    return list(
        difflib.unified_diff(
            before_text.splitlines(keepends=False),
            after_text.splitlines(keepends=False),
            fromfile="current (remote)",
            tofile="proposed",
            lineterm="",
        )
    )


@router.get("/customers/{customer_id}/assign")
async def admin_customer_assign_form(
    customer_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render the assign-to-server form with a diff preview.

    We pre-resolve the *recommended* server via
    :func:`services_distribution.resolve_target_server` (a read-only call —
    it doesn't write the assignment yet) and render the diff of the
    proposed ``config.ini`` against the current remote contents for the
    recommended stack. If the distribution policy is manual + no default,
    the resolver raises ``ValueError`` and we surface the form without a
    recommendation; the admin still has to pick a server explicitly.
    """
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")

    servers = await services_servers.list_servers(db)

    try:
        recommended = await services_distribution.resolve_target_server(
            db, customer, override_server_id=None
        )
    except ValueError:
        # Manual policy + no default + no override = no recommendation
        # is possible. The form still works — the admin just has to make
        # an explicit choice.
        recommended = None

    # Diff preview is only meaningful when we can resolve to a concrete
    # stack. If there's no recommendation (or the recommended server has
    # no provisioned stack for this customer's agent yet) we degrade to
    # an empty diff and the template shows a friendly fallback.
    diff_lines: list[str] = []
    diff_error: Optional[str] = None
    before_text = ""
    after_text = ""

    if recommended is not None:
        # Look up the (server, agent) stack so we can ask the rendering
        # layer for a preview. The stack is the unit of config.ini, not
        # the customer — a server can host many agents' stacks.
        from sqlalchemy import select as _select
        from app.models.stacks import AgentStack

        stmt = _select(AgentStack).where(
            AgentStack.server_id == recommended.id,
            AgentStack.agent_id == customer.agent_id,
        )
        result = await db.execute(stmt)
        stack = result.scalar_one_or_none()
        if stack is not None:
            try:
                before_text, after_text = (
                    await services_stacks.render_config_ini_for_stack_preview(
                        db, stack.id
                    )
                )
                diff_lines = _compute_config_diff(before_text, after_text)
            except SSHError as exc:
                # Server unreachable / SFTP refused / file missing — the
                # admin can still proceed with the assignment, the actual
                # push will surface the same error.
                diff_error = (
                    "Could not fetch the current remote file for preview: "
                    f"{exc}"
                )
        else:
            diff_error = (
                "No provisioned stack for this agent on the recommended "
                "server yet — one will be created when you assign."
            )

    ctx = _ctx(request, user, current_tab="/admin/customers")
    ctx["customer"] = customer
    ctx["servers"] = servers
    ctx["recommended"] = recommended
    ctx["diff_lines"] = diff_lines
    ctx["diff_error"] = diff_error
    ctx["before_text"] = before_text
    ctx["after_text"] = after_text
    return templates.TemplateResponse("admin/customer_assign.html", ctx)


@router.post("/customers/{customer_id}/assign")
async def admin_customer_assign(
    customer_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    server_id: UUID = Form(...),
):
    """Place a customer on a server.

    The service does all the heavy lifting (stack lookup or creation,
    config.ini render + SFTP push, audit). On success we redirect to the
    customer detail page; HTMX callers get an ``HX-Redirect`` instead of
    a hard 303 so the spinner spinner state finishes cleanly.
    """
    try:
        await services_distribution.assign_customer(
            db, customer_id, server_id=server_id, actor_id=user.id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # Service-level invariant (e.g. agent has no stack on this
        # server, or the server is offline). Surface as 400 with the
        # service's own message — these are domain errors, not
        # pydantic-style validation errors, so str(exc) is safe.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SSHError as exc:
        # The DB assignment already committed inside assign_customer; only
        # the on-server config.ini push failed (host unreachable, key
        # rejected, stack dir not yet provisioned, …). Surface as a soft
        # warning by logging + redirecting to the detail page — the admin
        # can see status='active' in DB and retry the push via the stack
        # detail's Redeploy when the server is reachable.
        logger.warning(
            "assign_customer: SSH push failed customer=%s server=%s: %s",
            customer_id, server_id, exc,
        )

    return _flash_redirect(request, f"/admin/customers/{customer_id}")


@router.post("/customers/{customer_id}/unassign")
async def admin_customer_unassign(
    customer_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Detach a customer from its current server.

    Resets ``assignment_status='pending'`` and ``stack_id=NULL``, then
    re-renders the old stack's ``config.ini`` (without this customer's
    section) and SFTPs it out so the on-server bot stops trading the
    instrument immediately.
    """
    try:
        await services_distribution.unassign_customer(
            db, customer_id, actor_id=user.id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SSHError as exc:
        # DB unassignment already committed; only the old stack's
        # config.ini push failed. Log + redirect (same pattern as assign).
        logger.warning("unassign_customer: SSH push failed customer=%s: %s", customer_id, exc)

    return _flash_redirect(request, f"/admin/customers/{customer_id}")


@router.post("/customers/{customer_id}/move")
async def admin_customer_move(
    customer_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    new_server_id: UUID = Form(...),
):
    """Move a customer to a different server.

    Both the old and the new stack get a fresh ``config.ini`` push (the
    old one loses the section, the new one gains it). The service emits
    a single audit row with both stack ids in ``affected_stack_ids``.
    """
    try:
        await services_distribution.move_customer(
            db, customer_id, new_server_id=new_server_id, actor_id=user.id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SSHError as exc:
        # DB move already committed; one or both stack pushes failed. The
        # admin can retry via the stack detail's Redeploy on the affected
        # servers — the DB is the source of truth and the next push will
        # converge.
        logger.warning("move_customer: SSH push failed customer=%s: %s", customer_id, exc)

    return _flash_redirect(request, f"/admin/customers/{customer_id}")


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
