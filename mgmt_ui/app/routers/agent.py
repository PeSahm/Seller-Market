"""Agent (and admin-as-agent) HTML routes.

Per the plan, admins may also reach `/agent/*` pages so they can act as
any agent. We therefore use `get_current_user` + an inline role check
rather than a strict `require_agent`-only guard (which would already
allow admins, but we keep the explicit check for clarity).

Phase 4 ships agent-scoped customer CRUD here. Every customer route must
filter by ``current_user.id`` for agents: an agent who learns another
agent's customer UUID and crafts a direct GET/POST must get 404, not the
data. Admins can also reach these pages (the plan allows "admin acts as
agent"); see :func:`_can_access_customer`.

Secret hygiene: customer passwords NEVER round-trip through the HTML.
They are accepted on the form, Fernet-encrypted by the service layer
into ``password_enc``, and dropped. Form re-render on validation error
keeps every other field except the password; the user has to retype it.
"""
from __future__ import annotations

import difflib
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.customers import Customer
from app.models.stacks import AgentStack
from app.models.users import User
from app.routers.dashboard import _ctx, templates
from app.schemas.customer import (
    BROKERS,
    CustomerCreate,
    CustomerDuplicate,
    CustomerUpdate,
)
from app.schemas.locust import LocustUpsert
from app.schemas.scheduler import SchedulerJobUpsert
from app.security.deps import get_current_user
from app.services import agents as services_agents
from app.services import customers as services_customers
from app.services import locust_configs as services_locust
from app.services import run_executor
from app.services import runs as services_runs
from app.services import scheduler_jobs as services_scheduler
from app.services import servers as services_servers
from app.services import settings_store
from app.services import stacks as services_stacks
from app.services.customers import OptimisticLockError
from app.services.locust_configs import OptimisticLockError as LocustLockError
from app.services.run_locks import StackRunLockBusyError
from app.services.scheduler_jobs import OptimisticLockError as SchedulerLockError
from app.services.ssh.exceptions import SSHError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent-ui"], include_in_schema=False)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _require_agent_or_admin(user: User) -> None:
    if user.role not in ("agent", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


def _can_access_customer(user: User, customer: Customer) -> bool:
    """Admin can act as any agent; an agent may only see/edit their own row.

    The router uses this to decide whether to surface a 404 (NOT a 403):
    we deliberately don't tell an agent that *some other* agent's customer
    exists — that would leak the UUID space.
    """
    return user.role == "admin" or customer.agent_id == user.id


def _render(request: Request, user: User, template_name: str, current_tab: str):
    return templates.TemplateResponse(
        template_name, _ctx(request, user, current_tab=current_tab)
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/dashboard")
async def agent_dashboard(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Agent overview. Cards backed by live data once their phase has shipped.

    The "My customers" + "Pending" cards are wired here (Phase 4). For admins
    acting as agent we show the *whole fleet*'s customer summary rather than a
    single agent's — same convention as the agent customer list.
    """
    _require_agent_or_admin(user)
    my_customers = await services_customers.list_customers(
        db,
        agent_id=user.id if user.role == "agent" else None,
        include_disabled=False,
    )
    customer_summary = {
        "total": len(my_customers),
        "active": sum(
            1 for c in my_customers if c.assignment_status == "active"
        ),
        "pending": sum(
            1 for c in my_customers if c.assignment_status == "pending"
        ),
    }
    # Phase 6: "Recent runs" card on the agent dashboard. Admins acting as
    # agent see the whole fleet (same convention as the customers card above);
    # agents only see their own runs. Cap at 100 since we just compute counts.
    own_runs = await services_runs.list_runs(
        db,
        agent_id=user.id if user.role != "admin" else None,
        limit=100,
    )
    runs_summary = {
        "total": len(own_runs),
        "running": sum(1 for r in own_runs if r.status == "running"),
        "success": sum(1 for r in own_runs if r.status == "success"),
        "failed": sum(
            1 for r in own_runs if r.status in ("failed", "killed")
        ),
    }
    ctx = _ctx(request, user, current_tab="/agent/dashboard")
    ctx["customer_summary"] = customer_summary
    ctx["runs_summary"] = runs_summary
    return templates.TemplateResponse("agent/dashboard.html", ctx)


@router.get("/stacks")
async def agent_stacks(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the current agent's stacks (one per server they've been deployed on).

    Mirrors the admin stacks list but filters by ``agent_id`` for agents and
    shows the whole fleet to admins acting-as-agent. Each row exposes the
    Phase-5 editors ("Edit schedule" / "Edit locust") so the agent doesn't
    have to round-trip through admin to tune their own bot.
    """
    _require_agent_or_admin(user)
    all_stacks = await services_stacks.list_stacks(db)
    if user.role == "agent":
        stacks = [s for s in all_stacks if s.agent_id == user.id]
    else:
        stacks = all_stacks
    servers_by_id = {
        s.id: s for s in await services_servers.list_servers(db)
    }
    ctx = _ctx(request, user, current_tab="/agent/stacks")
    ctx["stacks"] = stacks
    ctx["servers_by_id"] = servers_by_id
    return templates.TemplateResponse("agent/stacks.html", ctx)


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
    """Permanent redirect: the Phase-6 "Runs" page replaces the old placeholder.

    Kept so any bookmarks or in-flight links to ``/agent/logs`` still land
    somewhere useful instead of 404'ing.
    """
    _require_agent_or_admin(user)
    return Response(
        status_code=status.HTTP_308_PERMANENT_REDIRECT,
        headers={"Location": "/agent/runs"},
    )


# ---------------------------------------------------------------------------
# Customers (Phase 4)
# ---------------------------------------------------------------------------


@router.get("/customers")
async def agent_customers(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
):
    """List the current agent's customers. Admin sees all; agents see only theirs.

    ``status`` query param (linked from the dashboard's "N pending" badge)
    narrows the list to ``pending`` / ``assigned`` / ``active`` rows.
    Any other / empty value is treated as "no filter".

    We pre-load the server lookup dict so the table can render a read-only
    "Server" badge column without lazy-loading per row.
    """
    _require_agent_or_admin(user)
    status_filter = status if status in {"pending", "assigned", "active"} else None
    if user.role == "admin":
        customers = await services_customers.list_customers(
            db, status=status_filter, include_disabled=False
        )
    else:
        customers = await services_customers.list_customers(
            db, agent_id=user.id, status=status_filter, include_disabled=False
        )
    servers_by_id = {s.id: s for s in await services_servers.list_servers(db)}
    ctx = _ctx(request, user, current_tab="/agent/customers")
    ctx["customers"] = customers
    ctx["servers_by_id"] = servers_by_id
    ctx["filter_status"] = status_filter
    return templates.TemplateResponse("agent/customers.html", ctx)


@router.get("/customers/new")
async def agent_customer_new(
    request: Request,
    user: User = Depends(get_current_user),
):
    """Render the "add customer" form with empty values."""
    _require_agent_or_admin(user)
    ctx = _ctx(request, user, current_tab="/agent/customers")
    ctx["form_error"] = None
    ctx["form_values"] = {}
    ctx["brokers"] = BROKERS
    ctx["mode"] = "create"
    return templates.TemplateResponse("agent/customer_form.html", ctx)


@router.post("/customers")
async def agent_customer_create(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    display_name: str = Form(...),
    broker: str = Form(...),
    isin: str = Form(...),
    side: int = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    comment: Optional[str] = Form(None),
):
    """Create a new customer owned by the current agent.

    Pydantic validates shape (ISIN charset, side range, broker enum). The
    service layer enforces the composite UNIQUE on
    ``(agent_id, username, broker, isin, side)`` and raises ``ValueError`` on
    collision; we surface that as an inline form error.

    Note: ``password`` is intentionally dropped from ``form_values`` on
    re-render — secrets MUST NOT round-trip through the HTML.
    """
    _require_agent_or_admin(user)

    sticky = {
        "display_name": display_name,
        "broker": broker,
        "isin": isin,
        "side": side,
        "username": username,
        "comment": comment,
    }

    try:
        payload = CustomerCreate(
            display_name=display_name,
            broker=broker,
            isin=isin,
            side=side,
            username=username,
            password=password,
            comment=comment,
        )
    except (ValidationError, ValueError) as exc:
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["form_error"] = (
            "Invalid input. Please review the form fields and try again."
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        ctx["form_values"] = sticky
        ctx["brokers"] = BROKERS
        ctx["mode"] = "create"
        return templates.TemplateResponse(
            "agent/customer_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Admins acting-as-agent: in Phase 4 there is no "act-as" session yet, so
    # an admin creating from /agent/customers/new owns the row themselves.
    # The act-as flow (Phase 4 also, separate agent) will override agent_id.
    try:
        customer = await services_customers.create_customer(
            db, agent_id=user.id, data=payload, actor_id=user.id
        )
    except ValueError as exc:
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["form_error"] = str(exc)
        ctx["form_values"] = sticky
        ctx["brokers"] = BROKERS
        ctx["mode"] = "create"
        return templates.TemplateResponse(
            "agent/customer_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    redirect_to = f"/agent/customers/{customer.id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


@router.get("/customers/{customer_id}")
async def agent_customer_detail(
    customer_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the per-customer detail page.

    Cross-tenant isolation: an agent requesting another agent's UUID gets a
    404 (NOT 403). We do NOT want to leak existence of other tenants' rows.
    """
    _require_agent_or_admin(user)
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or not _can_access_customer(user, customer):
        raise HTTPException(status_code=404, detail="customer not found")
    server = None
    if customer.server_id:
        server = await services_servers.get_server(db, customer.server_id)
    ctx = _ctx(request, user, current_tab="/agent/customers")
    ctx["customer"] = customer
    ctx["server"] = server
    return templates.TemplateResponse("agent/customer_detail.html", ctx)


@router.get("/customers/{customer_id}/edit")
async def agent_customer_edit_form(
    customer_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the "edit customer" form pre-filled with current values.

    Password is intentionally NOT pre-filled — the agent leaves it empty to
    keep the current password, or types a new one to rotate.
    """
    _require_agent_or_admin(user)
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or not _can_access_customer(user, customer):
        raise HTTPException(status_code=404, detail="customer not found")
    ctx = _ctx(request, user, current_tab="/agent/customers")
    ctx["customer"] = customer
    ctx["form_error"] = None
    ctx["form_values"] = {
        "display_name": customer.display_name,
        "broker": customer.broker,
        "isin": customer.isin,
        "side": customer.side,
        "username": customer.username,
        # NEVER pre-fill password — agent leaves empty to keep current.
        "comment": customer.comment,
        "enabled": customer.enabled,
    }
    ctx["brokers"] = BROKERS
    ctx["mode"] = "edit"
    return templates.TemplateResponse("agent/customer_form.html", ctx)


@router.post("/customers/{customer_id}/edit")
async def agent_customer_update(
    customer_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    display_name: Optional[str] = Form(None),
    broker: Optional[str] = Form(None),
    isin: Optional[str] = Form(None),
    side: Optional[int] = Form(None),
    username: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    comment: Optional[str] = Form(None),
    enabled: Optional[str] = Form(None),
    version: int = Form(...),
):
    """Apply a partial, optimistic-locked update.

    Empty ``password`` form field → caller didn't rotate; we drop the field
    from the update payload (vs. setting it to empty, which the schema would
    reject anyway via ``min_length=1``).

    ``enabled`` is a checkbox: HTML form sends ``"on"`` when checked and
    omits the field when unchecked, so a missing value means "disable".
    """
    _require_agent_or_admin(user)
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or not _can_access_customer(user, customer):
        raise HTTPException(status_code=404, detail="customer not found")

    # Build CustomerUpdate kwargs, dropping fields the form didn't send and
    # handling the password "leave blank = no change" rule.
    update_kwargs: dict = {}
    if display_name is not None:
        update_kwargs["display_name"] = display_name
    if broker is not None:
        update_kwargs["broker"] = broker
    if isin is not None:
        update_kwargs["isin"] = isin
    if side is not None:
        update_kwargs["side"] = side
    if username is not None:
        update_kwargs["username"] = username
    if password:  # truthy → caller wants to rotate; empty string is dropped
        update_kwargs["password"] = password
    if comment is not None:
        update_kwargs["comment"] = comment
    # enabled checkbox: "on" if checked, absent (None) if unchecked.
    update_kwargs["enabled"] = enabled == "on"

    # Sticky values for any re-render (NEVER include password).
    sticky = {
        "display_name": display_name if display_name is not None else customer.display_name,
        "broker": broker if broker is not None else customer.broker,
        "isin": isin if isin is not None else customer.isin,
        "side": side if side is not None else customer.side,
        "username": username if username is not None else customer.username,
        "comment": comment if comment is not None else customer.comment,
        "enabled": update_kwargs["enabled"],
    }

    try:
        payload = CustomerUpdate(version=version, **update_kwargs)
    except (ValidationError, ValueError) as exc:
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["form_error"] = (
            "Invalid input. Please review the form fields and try again."
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        ctx["form_values"] = sticky
        ctx["brokers"] = BROKERS
        ctx["mode"] = "edit"
        ctx["customer"] = customer
        return templates.TemplateResponse(
            "agent/customer_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        await services_customers.update_customer(
            db, customer_id, payload, actor_id=user.id
        )
    except OptimisticLockError:
        # Another change won the race. Tell the user, return 409.
        # Re-read the row so the form shows the *current* server-side state
        # (including the bumped version) on retry — but re-verify tenant
        # access against the refreshed row. If a future move-customer-
        # between-agents flow happened mid-edit, ownership might have
        # changed; we must NOT leak a row the user no longer owns.
        fresh = await services_customers.get_customer(db, customer_id)
        if fresh is None or not _can_access_customer(user, fresh):
            raise HTTPException(status_code=404, detail="customer not found")
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["form_error"] = (
            "Another change won the race. Reload the page and try again."
        )
        ctx["form_values"] = sticky
        ctx["brokers"] = BROKERS
        ctx["mode"] = "edit"
        ctx["customer"] = fresh
        return templates.TemplateResponse(
            "agent/customer_form.html",
            ctx,
            status_code=status.HTTP_409_CONFLICT,
        )
    except ValueError as exc:
        # Composite UNIQUE collision — surface as an inline form error.
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["form_error"] = str(exc)
        ctx["form_values"] = sticky
        ctx["brokers"] = BROKERS
        ctx["mode"] = "edit"
        ctx["customer"] = customer
        return templates.TemplateResponse(
            "agent/customer_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except LookupError as exc:
        # Race: row vanished between the access check and the update. Treat
        # as 404 for consistency with cross-tenant isolation.
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    redirect_to = f"/agent/customers/{customer_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


@router.post("/customers/{customer_id}/delete")
async def agent_customer_delete(
    customer_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a customer (the service flips ``enabled=False``)."""
    _require_agent_or_admin(user)
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or not _can_access_customer(user, customer):
        raise HTTPException(status_code=404, detail="customer not found")
    await services_customers.soft_delete_customer(
        db, customer_id, actor_id=user.id
    )
    if request.headers.get("HX-Request"):
        return Response(
            status_code=204, headers={"HX-Redirect": "/agent/customers"}
        )
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": "/agent/customers"},
    )


@router.get("/customers/{customer_id}/duplicate")
async def agent_customer_duplicate_form(
    customer_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the minimal "duplicate to new ISIN" form.

    Only asks for the new ISIN (+ optional new display name). Broker, side,
    username and password ciphertext are inherited from the source.
    """
    _require_agent_or_admin(user)
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or not _can_access_customer(user, customer):
        raise HTTPException(status_code=404, detail="customer not found")
    ctx = _ctx(request, user, current_tab="/agent/customers")
    ctx["source"] = customer
    ctx["form_error"] = None
    ctx["form_values"] = {}
    return templates.TemplateResponse("agent/customer_duplicate.html", ctx)


@router.post("/customers/{customer_id}/duplicate")
async def agent_customer_duplicate(
    customer_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    isin: str = Form(...),
    new_display_name: Optional[str] = Form(None),
):
    """Clone a customer to a new ISIN.

    The service layer carries over broker, side, username, and Fernet
    ciphertext from the source — we don't decrypt to clone.
    """
    _require_agent_or_admin(user)
    source = await services_customers.get_customer(db, customer_id)
    if source is None or not _can_access_customer(user, source):
        raise HTTPException(status_code=404, detail="customer not found")

    # Normalize blank display name to None so the service layer falls back to
    # the "<source.display_name> (<new_isin>)" default.
    normalized_name = (
        new_display_name.strip() if new_display_name else None
    ) or None

    sticky = {"isin": isin, "new_display_name": new_display_name or ""}

    try:
        payload = CustomerDuplicate(
            isin=isin, new_display_name=normalized_name
        )
    except (ValidationError, ValueError) as exc:
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["source"] = source
        ctx["form_error"] = (
            "Invalid input. Please review the form fields and try again."
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        ctx["form_values"] = sticky
        return templates.TemplateResponse(
            "agent/customer_duplicate.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        new_customer = await services_customers.duplicate_customer(
            db, source.id, payload, actor_id=user.id
        )
    except ValueError as exc:
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["source"] = source
        ctx["form_error"] = str(exc)
        ctx["form_values"] = sticky
        return templates.TemplateResponse(
            "agent/customer_duplicate.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except LookupError as exc:
        # Source disappeared between the access check and the duplicate
        # service call. Surface as 404.
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    redirect_to = f"/agent/customers/{new_customer.id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


# ---------------------------------------------------------------------------
# Stacks scheduler + locust (Phase 5)
# ---------------------------------------------------------------------------
#
# Per-stack scheduler / locust editors scoped to the *current* agent. The
# admin equivalents live in :mod:`app.routers.admin`; we mirror the routes
# here so an agent can self-serve without an admin acting as them.
#
# Tenant guard: every route loads the stack first, then refuses (404) unless
# ``stack.agent_id == user.id`` (or the caller is an admin). We deliberately
# surface 404 rather than 403 so an agent guessing UUIDs can't enumerate the
# stacks of other tenants.
#
# Templates are SHARED with the admin pages (``admin/stack_scheduler.html``
# and ``admin/stack_locust.html``) — they don't hard-code their own URL
# prefix, the router passes ``save_url`` / ``save_url_prefix`` /
# ``back_url`` so the same template renders correctly under either prefix.


def _flash_redirect(request: Request, location: str) -> Response:
    """303 / HX-Redirect helper for the agent-side Phase-5 routes.

    Same shape as the admin module's helper (kept local so we don't reach
    across modules just for a 6-line redirect builder).
    """
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": location})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": location},
    )


def _compute_json_diff(before_text: str, after_text: str) -> list[str]:
    """Unified-diff helper for the scheduler / locust preview blocks.

    Local copy of the admin module's ``_compute_config_diff`` (without the
    ``config.ini`` password-redaction pass, which neither file needs). We
    keep a tiny local helper instead of cross-importing to avoid coupling
    the two router modules.
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


async def _load_stack_for_agent(
    db: AsyncSession, user: User, stack_id: UUID
):
    """Resolve a stack id and enforce the per-agent tenant guard.

    Returns the stack on success. Raises an ``HTTPException(404)`` on either
    "no such stack" OR "not your stack" — we never let an agent learn that
    *someone else's* stack id is valid.
    """
    stack = await services_stacks.get_stack(db, stack_id)
    if stack is None:
        raise HTTPException(status_code=404, detail="stack not found")
    if user.role != "admin" and stack.agent_id != user.id:
        raise HTTPException(status_code=404, detail="stack not found")
    return stack


@router.get("/stacks/{stack_id}/scheduler")
async def agent_stack_scheduler(
    stack_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the scheduler editor for one of the current agent's stacks.

    Tenant-guarded — see :func:`_load_stack_for_agent`. Template, context
    shape, and diff-preview behaviour are identical to the admin route; the
    only delta is the URL prefix (``/agent`` vs ``/admin``) passed in
    ``save_url_prefix`` so the template knows where the form posts.
    """
    _require_agent_or_admin(user)
    stack = await _load_stack_for_agent(db, user, stack_id)

    jobs = await services_scheduler.list_jobs(db, stack_id=stack_id)
    jobs_by_name = {j.name: j for j in jobs}

    before_text = ""
    after_text = ""
    diff_lines: list[str] = []
    diff_error: Optional[str] = None
    try:
        before_text, after_text = (
            await services_stacks.render_scheduler_config_for_stack_preview(
                db, stack_id
            )
        )
        diff_lines = _compute_json_diff(before_text, after_text)
    except SSHError as exc:
        diff_error = (
            "Could not fetch the current remote file for preview: "
            f"{exc}"
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    ctx = _ctx(request, user, current_tab="/agent/stacks")
    ctx["stack"] = stack
    ctx["jobs_by_name"] = jobs_by_name
    ctx["form_error"] = None
    ctx["form_values"] = {}
    ctx["before_text"] = before_text
    ctx["after_text"] = after_text
    ctx["diff_lines"] = diff_lines
    ctx["diff_error"] = diff_error
    ctx["save_url_prefix"] = f"/agent/stacks/{stack_id}/scheduler"
    ctx["back_url"] = "/agent/stacks"
    return templates.TemplateResponse("admin/stack_scheduler.html", ctx)


@router.post("/stacks/{stack_id}/scheduler/{name}")
async def agent_stack_scheduler_save(
    stack_id: UUID,
    name: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    time: str = Form(...),
    enabled: Optional[str] = Form(None),
    version: int = Form(...),
):
    """Upsert one of the two scheduler jobs (tenant-guarded).

    Same form semantics as the admin route: ``enabled`` is a checkbox
    ("on" or absent), ``version=0`` is the create sentinel. SSH push
    failure is best-effort.
    """
    _require_agent_or_admin(user)
    stack = await _load_stack_for_agent(db, user, stack_id)
    if name not in ("cache_warmup", "run_trading"):
        raise HTTPException(status_code=400, detail="unknown job name")

    enabled_bool = enabled == "on"

    async def _rerender(message: str, code: int):
        # Re-read both jobs so the un-edited form re-syncs to its current
        # row (with the fresh version). The edited form gets the sticky
        # values from the request payload.
        jobs = await services_scheduler.list_jobs(db, stack_id=stack_id)
        jobs_by_name = {j.name: j for j in jobs}
        before_text = ""
        after_text = ""
        diff_lines: list[str] = []
        diff_error: Optional[str] = None
        try:
            before_text, after_text = (
                await services_stacks.render_scheduler_config_for_stack_preview(
                    db, stack_id
                )
            )
            diff_lines = _compute_json_diff(before_text, after_text)
        except SSHError as exc:
            diff_error = (
                "Could not fetch the current remote file for preview: "
                f"{exc}"
            )
        except LookupError as exc:
            # Stack disappeared between the initial check and this
            # rerender (e.g. concurrent deprovision). Return 404 instead
            # of falling through to 500.
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        ctx = _ctx(request, user, current_tab="/agent/stacks")
        ctx["stack"] = stack
        ctx["jobs_by_name"] = jobs_by_name
        ctx["form_error"] = message
        ctx["form_values"] = {
            "name": name,
            "time": time,
            "enabled": enabled_bool,
            "version": version,
        }
        ctx["before_text"] = before_text
        ctx["after_text"] = after_text
        ctx["diff_lines"] = diff_lines
        ctx["diff_error"] = diff_error
        ctx["save_url_prefix"] = f"/agent/stacks/{stack_id}/scheduler"
        ctx["back_url"] = "/agent/stacks"
        return templates.TemplateResponse(
            "admin/stack_scheduler.html", ctx, status_code=code,
        )

    try:
        payload = SchedulerJobUpsert(
            time=time, enabled=enabled_bool, version=version,
        )
    except (ValidationError, ValueError) as exc:
        message = (
            "Invalid input. Please review the form fields and try again."
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        return await _rerender(message, status.HTTP_400_BAD_REQUEST)

    try:
        await services_scheduler.upsert_job(
            db, stack_id, name, payload, actor_id=user.id
        )
    except SchedulerLockError:
        return await _rerender(
            "This job was changed by someone else while you were editing. "
            "Reload the page and re-apply your changes.",
            status.HTTP_409_CONFLICT,
        )
    except ValueError as exc:
        return await _rerender(str(exc), status.HTTP_400_BAD_REQUEST)

    try:
        await services_stacks.push_scheduler_config_for_stack(
            db, stack_id, actor_id=user.id
        )
    except SSHError as exc:
        logger.warning(
            "agent_stack_scheduler_save: push failed stack=%s: %s",
            stack_id, exc,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _flash_redirect(request, f"/agent/stacks/{stack_id}/scheduler")


@router.get("/stacks/{stack_id}/locust")
async def agent_stack_locust(
    stack_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the locust-config editor for one of the current agent's stacks."""
    _require_agent_or_admin(user)
    stack = await _load_stack_for_agent(db, user, stack_id)

    locust = await services_locust.get_locust_config(db, stack_id)
    processes_cap = int(
        await settings_store.get_setting(db, "agent_locust_processes_cap")
    )

    before_text = ""
    after_text = ""
    diff_lines: list[str] = []
    diff_error: Optional[str] = None
    try:
        before_text, after_text = (
            await services_stacks.render_locust_config_for_stack_preview(
                db, stack_id
            )
        )
        diff_lines = _compute_json_diff(before_text, after_text)
    except SSHError as exc:
        diff_error = (
            "Could not fetch the current remote file for preview: "
            f"{exc}"
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    ctx = _ctx(request, user, current_tab="/agent/stacks")
    ctx["stack"] = stack
    ctx["locust"] = locust
    ctx["processes_cap"] = processes_cap
    ctx["form_error"] = None
    ctx["form_values"] = {}
    ctx["before_text"] = before_text
    ctx["after_text"] = after_text
    ctx["diff_lines"] = diff_lines
    ctx["diff_error"] = diff_error
    ctx["save_url"] = f"/agent/stacks/{stack_id}/locust"
    ctx["back_url"] = "/agent/stacks"
    return templates.TemplateResponse("admin/stack_locust.html", ctx)


@router.post("/stacks/{stack_id}/locust")
async def agent_stack_locust_save(
    stack_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    users: int = Form(...),
    spawn_rate: int = Form(...),
    run_time: str = Form(...),
    host: str = Form(...),
    processes: int = Form(...),
    version: int = Form(...),
):
    """Upsert the locust config row for the current agent's stack."""
    _require_agent_or_admin(user)
    stack = await _load_stack_for_agent(db, user, stack_id)

    sticky = {
        "users": users,
        "spawn_rate": spawn_rate,
        "run_time": run_time,
        "host": host,
        "processes": processes,
        "version": version,
    }

    async def _rerender(message: str, code: int):
        locust = await services_locust.get_locust_config(db, stack_id)
        processes_cap = int(
            await settings_store.get_setting(
                db, "agent_locust_processes_cap"
            )
        )
        before_text = ""
        after_text = ""
        diff_lines: list[str] = []
        diff_error: Optional[str] = None
        try:
            before_text, after_text = (
                await services_stacks.render_locust_config_for_stack_preview(
                    db, stack_id
                )
            )
            diff_lines = _compute_json_diff(before_text, after_text)
        except SSHError as exc:
            diff_error = (
                "Could not fetch the current remote file for preview: "
                f"{exc}"
            )
        except LookupError as exc:
            # Stack disappeared between the initial check and this
            # rerender (e.g. concurrent deprovision). Return 404 instead
            # of falling through to 500.
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        ctx = _ctx(request, user, current_tab="/agent/stacks")
        ctx["stack"] = stack
        ctx["locust"] = locust
        ctx["processes_cap"] = processes_cap
        ctx["form_error"] = message
        ctx["form_values"] = sticky
        ctx["before_text"] = before_text
        ctx["after_text"] = after_text
        ctx["diff_lines"] = diff_lines
        ctx["diff_error"] = diff_error
        ctx["save_url"] = f"/agent/stacks/{stack_id}/locust"
        ctx["back_url"] = "/agent/stacks"
        return templates.TemplateResponse(
            "admin/stack_locust.html", ctx, status_code=code,
        )

    try:
        payload = LocustUpsert(
            users=users,
            spawn_rate=spawn_rate,
            run_time=run_time,
            host=host,
            processes=processes,
            version=version,
        )
    except (ValidationError, ValueError) as exc:
        message = (
            "Invalid input. Please review the form fields and try again."
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        return await _rerender(message, status.HTTP_400_BAD_REQUEST)

    try:
        await services_locust.upsert_locust_config(
            db, stack_id, payload, actor_id=user.id
        )
    except LocustLockError:
        return await _rerender(
            "This locust config was changed by someone else while you were "
            "editing. Reload the page and re-apply your changes.",
            status.HTTP_409_CONFLICT,
        )
    except ValueError as exc:
        return await _rerender(str(exc), status.HTTP_400_BAD_REQUEST)

    try:
        await services_stacks.push_locust_config_for_stack(
            db, stack_id, actor_id=user.id
        )
    except SSHError as exc:
        logger.warning(
            "agent_stack_locust_save: push failed stack=%s: %s",
            stack_id, exc,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _flash_redirect(request, f"/agent/stacks/{stack_id}/locust")


# ---------------------------------------------------------------------------
# Stack detail + runs (Phase 6)
# ---------------------------------------------------------------------------
#
# The Phase-6 "run now" flow lives entirely on the agent side: an agent picks
# one of their own stacks, hits "Run cache_warmup" or "Run trading", and the
# router fires :func:`run_executor.start_manual_run`. The executor doesn't do
# RBAC — tenant scoping happens HERE, in this router, by loading the stack
# first and bailing with 404 (NOT 403) when the caller is an agent who
# doesn't own it. The same pattern guards the runs list + detail endpoints.
#
# The live-log WebSocket (``/ws/runs/{run_id}``) is owned by parallel agent B
# in :mod:`app.routers.ws`; we just point the detail template at that URL.


@router.get("/stacks/{stack_id}")
async def agent_stack_detail(
    stack_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-stack detail page for an agent.

    Surfaces the Phase-5 editors (scheduler / locust) and the Phase-6 "run
    now" buttons + a small "recent runs for this stack" panel.

    Tenant-guarded: :func:`_load_stack_for_agent` raises 404 if the caller
    is an agent and the stack belongs to someone else — we never let an
    agent learn that another tenant's stack id is valid.
    """
    _require_agent_or_admin(user)
    stack = await _load_stack_for_agent(db, user, stack_id)
    server = await services_servers.get_server(db, stack.server_id)
    agent_user = await services_agents.get_agent(db, stack.agent_id)
    stack_runs = await services_runs.list_runs(db, stack_id=stack_id, limit=10)
    ctx = _ctx(request, user, current_tab="/agent/stacks")
    ctx["stack"] = stack
    ctx["server"] = server
    ctx["agent"] = agent_user
    ctx["stack_runs"] = stack_runs
    return templates.TemplateResponse("agent/stack_detail.html", ctx)


@router.post("/stacks/{stack_id}/run/{job_name}")
async def agent_stack_run_now(
    stack_id: UUID,
    job_name: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fire a manual run on one of the current agent's stacks.

    Re-uses the same :func:`run_executor.start_manual_run` as a hypothetical
    admin path — RBAC is *not* the executor's job. Tenant scoping is done
    HERE: if the caller is an agent and the stack belongs to someone else
    we 404 (NOT 403) so we don't leak the existence of other tenants'
    stacks via UUID enumeration.

    Lock-busy is surfaced as 409 ("another run already in flight") so the
    UI can render a "try again in a moment" toast rather than treating it
    as a server error.
    """
    _require_agent_or_admin(user)
    if job_name not in ("cache_warmup", "run_trading"):
        raise HTTPException(
            status_code=400, detail=f"unknown job_name: {job_name}"
        )
    # _load_stack_for_agent does both the existence check AND the
    # cross-tenant 404 — see the helper for the exact comparison.
    stack = await _load_stack_for_agent(db, user, stack_id)
    try:
        run = await run_executor.start_manual_run(
            stack_id=stack.id,
            agent_id=stack.agent_id,
            job_name=job_name,
            actor_id=user.id,
        )
    except StackRunLockBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    redirect_to = f"/agent/runs/{run.id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


@router.get("/runs")
async def agent_runs(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    stack_id: Optional[str] = None,
    job_name: Optional[str] = None,
    status: Optional[str] = None,  # noqa: A002 — query param name
):
    """List the current agent's own runs.

    Admins see the whole fleet (so they can debug across tenants). Agents
    see only runs whose ``agent_id`` equals their user id — the filter is
    applied at the service layer, NOT post-hoc, so we never load other
    tenants' rows into memory.

    The three query params (``stack_id`` / ``job_name`` / ``status``) are
    empty-string tolerant: blank values are treated as "no filter" rather
    than 422, so the filter form can submit ``?status=`` for "all".
    """
    _require_agent_or_admin(user)

    filter_stack: Optional[UUID]
    if stack_id:
        try:
            filter_stack = UUID(stack_id)
        except ValueError:
            # Bad UUID in the URL bar — treat as "no filter" rather than 422.
            filter_stack = None
    else:
        filter_stack = None
    filter_job = job_name if job_name in ("cache_warmup", "run_trading") else None
    filter_status = (
        status if status in ("running", "success", "failed", "killed") else None
    )

    own_agent_id = None if user.role == "admin" else user.id
    runs = await services_runs.list_runs(
        db,
        agent_id=own_agent_id,
        stack_id=filter_stack,
        status=filter_status,
        limit=200,
    )
    if filter_job:
        runs = [r for r in runs if r.job_name == filter_job]

    # Pre-load the stacks lookup so the template can render each row's
    # compose_project without lazy-loading per row. Filter the dict to
    # only the stacks the caller is allowed to see (admins: all).
    all_stacks = await services_stacks.list_stacks(db)
    stacks_by_id = {
        s.id: s
        for s in all_stacks
        if user.role == "admin" or s.agent_id == user.id
    }

    ctx = _ctx(request, user, current_tab="/agent/runs")
    ctx["runs"] = runs
    ctx["stacks_by_id"] = stacks_by_id
    ctx["filter_stack_id"] = stack_id or ""
    ctx["filter_job"] = job_name or ""
    ctx["filter_status"] = status or ""
    ctx["all_stacks"] = sorted(
        stacks_by_id.values(), key=lambda s: s.compose_project
    )
    return templates.TemplateResponse("agent/runs.html", ctx)


@router.get("/runs/{run_id}")
async def agent_run_detail(
    run_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-run detail page (live log via WS while running, archived after).

    Tenant-guarded via :func:`services_runs.can_user_see_run` — same 404
    masking pattern as the rest of this module (we never tell an agent
    that *some other agent's* run id is valid).
    """
    _require_agent_or_admin(user)
    run = await services_runs.get_run(db, run_id)
    if run is None or not services_runs.can_user_see_run(user, run):
        raise HTTPException(status_code=404, detail="run not found")

    stack = await services_stacks.get_stack(db, run.stack_id)
    agent_user = await services_agents.get_agent(db, run.agent_id)

    # Archived log is only relevant once the run has finished; while
    # ``running`` the template opens a WebSocket and streams stdout live
    # (the WS handler will replay any already-buffered output).
    archived_log = ""
    if run.status != "running":
        bs = await services_runs.read_run_log(run)
        archived_log = bs.decode("utf-8", errors="replace")

    ctx = _ctx(request, user, current_tab="/agent/runs")
    ctx["run"] = run
    ctx["stack"] = stack
    # NOTE: we pass ``agent`` (the stack owner's User row) so the template
    # can show the *username* — never the raw UUID, which would leak
    # tenant info to anyone over-the-shoulder.
    ctx["agent"] = agent_user
    ctx["archived_log"] = archived_log
    return templates.TemplateResponse("agent/run_detail.html", ctx)


# Silence unused-import lint for AgentStack + select — referenced indirectly
# via services_stacks; kept here for forward use by an "agent stack detail"
# page in a later phase.
_ = AgentStack
_ = select
