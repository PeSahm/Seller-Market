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

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.customers import Customer
from app.models.users import User
from app.routers.dashboard import _ctx, templates
from app.schemas.customer import (
    BROKERS,
    CustomerCreate,
    CustomerDuplicate,
    CustomerUpdate,
)
from app.security.deps import get_current_user
from app.services import customers as services_customers
from app.services import servers as services_servers
from app.services.customers import OptimisticLockError

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
    ctx = _ctx(request, user, current_tab="/agent/dashboard")
    ctx["customer_summary"] = customer_summary
    return templates.TemplateResponse("agent/dashboard.html", ctx)


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
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["form_error"] = (
            "Another change won the race. Reload the page and try again."
        )
        ctx["form_values"] = sticky
        ctx["brokers"] = BROKERS
        ctx["mode"] = "edit"
        # Re-read the row so the form shows the *current* server-side state
        # (including the bumped version) on retry.
        fresh = await services_customers.get_customer(db, customer_id)
        ctx["customer"] = fresh if fresh is not None else customer
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
