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
from datetime import date, datetime, time, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.audit import AuditLog
from app.models.customers import Customer
from app.models.runs import StackRunLock
from app.models.stacks import AgentStack
from app.models.users import User
from app.routers.dashboard import _ctx, templates
from app.schemas.customer import (
    CustomerCreate,
    CustomerUpdate,
)
from app.schemas.trade_instruction import (
    TradeInstructionCreate,
    TradeInstructionUpdate,
    map_side_form,
)
from app.schemas.locust import LocustUpsert
from app.schemas.scheduler import SchedulerJobUpsert
from app.security.deps import get_current_user
from app.services import agents as services_agents
from app.services import auto_sell_view as services_auto_sell_view
from app.services import broker_client
from app.services import brokers_admin
from app.services import customers as services_customers
from app.services import health_signals as services_health
from app.services import broker_orders as services_broker_orders
from app.services import locust_configs as services_locust
from app.services import market_data_client
from app.services import profit_report as services_profit_report
from app.services import run_executor
from app.services import runs as services_runs
from app.services import scheduler_jobs as services_scheduler
from app.services import servers as services_servers
from app.services import settings_store
from app.services import stacks as services_stacks
from app.services import trade_instructions as services_trade_instructions
from app.services import trades as services_trades
from app.services.customers import OptimisticLockError
from app.services.trade_instructions import (
    OptimisticLockError as TradeInstructionLockError,
)
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


def _parse_optional_int(value: Optional[str]) -> Optional[int]:
    """Lenient int parse for an optional number form field (empty/invalid → None)."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stacks_bulk_location(base: str, **params) -> str:
    """Build a stacks-list redirect URL carrying a bulk-action result summary.

    Mirrors the admin helper: every value is an int or a short known token
    (``fk`` / ``run`` / ``cache_warmup`` / ``run_trading``), so no
    percent-encoding is needed; the list template re-escapes via Jinja
    autoescape. ``None`` params are dropped.
    """
    parts = [f"{k}={v}" for k, v in params.items() if v is not None]
    return base + ("?" + "&".join(parts) if parts else "")


async def _stacks_visible_to(db: AsyncSession, user: User) -> list:
    """The stacks the caller may act on in bulk: an agent's own, or all (admin).

    Matches exactly what :func:`agent_stacks` renders, so "Run all" /
    "Force kill all" operate on the set the user sees — never more.
    """
    all_stacks = await services_stacks.list_stacks(db)
    if user.role == "agent":
        return [s for s in all_stacks if s.agent_id == user.id]
    return all_stacks


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


async def _push_customer_stack_config(
    db: AsyncSession,
    customer_id: UUID,
    *,
    actor_id: UUID,
) -> None:
    """Re-push the assigned stack's config.ini after a customer mutation.

    ``services_customers.update_customer`` only commits the DB row — it
    does NOT touch the trading bot's on-disk ``config.ini``. Without this
    follow-up call, a "save then run" sequence reads the OLD field values
    on the bot side (e.g. edit password → click Run → bot still uses the
    previous password). Best-effort: SSH errors are logged but never
    re-raised — the DB row has already committed.
    """
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or customer.stack_id is None:
        return
    try:
        await services_stacks.push_config_ini_for_stack(
            db, stack_id=customer.stack_id, actor_id=actor_id
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception(
            "config.ini push failed after customer update %s "
            "(row committed; operator can retry from stack page)",
            customer_id,
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
    # Phase 7: Trade history card on the dashboard. Same convention as runs
    # above — admins acting as agent see the whole fleet, agents see only
    # their own trades. We cap the read at 100 because we only need counts.
    own_trades = await services_trades.list_trades(
        db,
        agent_id=user.id if user.role != "admin" else None,
        limit=100,
    )
    trades_summary = {
        "total": len(own_trades),
        "done": sum(1 for t in own_trades if t.is_done),
        "pending": sum(1 for t in own_trades if not t.is_done),
    }
    # Phase 7 follow-up: "My stacks" card. Agents see only their own
    # stacks; admins acting as agent see the whole fleet. Same convention
    # as runs and trades above.
    all_stacks = await services_stacks.list_stacks(db)
    my_stacks = (
        all_stacks
        if user.role == "admin"
        else [s for s in all_stacks if s.agent_id == user.id]
    )
    stacks_summary = {
        "total": len(my_stacks),
        "up": sum(1 for s in my_stacks if s.status == "up"),
        "down": sum(1 for s in my_stacks if s.status == "down"),
        "provisioning": sum(
            1 for s in my_stacks if s.status == "provisioning"
        ),
    }
    # Phase 8: unacked health-signals roll-up scoped to the agent's stacks.
    # Admins acting-as-agent see the whole fleet (same convention as the
    # runs and trades cards above); agents see only signals tagged to one
    # of their own stacks. We deliberately drop the "info" tier from the
    # agent card — info-level signals are operational noise the agent
    # can't act on (the admin needs to ack them anyway). The list page
    # surfaces info-level rows when the agent visits /agent/health.
    my_stack_ids = {s.id for s in my_stacks}
    # Tenant scope at the SQL layer (stack_ids=...) rather than after a
    # global LIMIT — a noisy stack from another agent could otherwise
    # push this agent's own unacked rows off the bottom of the 200-row
    # window. Admin sees the whole fleet (stack_ids=None).
    own_unacked = await services_health.list_signals(
        db,
        acked=False,
        stack_ids=None if user.role == "admin" else my_stack_ids,
        limit=200,
    )
    health_summary = {
        "unacked_total": len(own_unacked),
        "critical": sum(1 for s in own_unacked if s.severity == "critical"),
        "error": sum(1 for s in own_unacked if s.severity == "error"),
        "warning": sum(1 for s in own_unacked if s.severity == "warning"),
    }
    ctx = _ctx(request, user, current_tab="/agent/dashboard")
    ctx["customer_summary"] = customer_summary
    ctx["runs_summary"] = runs_summary
    ctx["trades_summary"] = trades_summary
    ctx["stacks_summary"] = stacks_summary
    ctx["health_summary"] = health_summary
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


@router.get("/auto-sell")
async def agent_auto_sell(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Active auto-sell for the agent's own armed positions (admin sees all).

    Live buy-queue refreshes every 3s via the rows partial (HTMX poll).
    """
    _require_agent_or_admin(user)
    own_agent_id = None if user.role == "admin" else user.id
    rows = await services_auto_sell_view.build_auto_sell_rows(db, agent_id=own_agent_id)
    ctx = _ctx(request, user, current_tab="/agent/auto-sell")
    ctx["rows"] = rows
    ctx["rows_url"] = "/agent/auto-sell/rows"
    return templates.TemplateResponse("agent/auto_sell.html", ctx)


@router.get("/auto-sell/rows")
async def agent_auto_sell_rows(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """HTMX-polled table body for the agent Active auto-sell page."""
    _require_agent_or_admin(user)
    own_agent_id = None if user.role == "admin" else user.id
    rows = await services_auto_sell_view.build_auto_sell_rows(db, agent_id=own_agent_id)
    ctx = _ctx(request, user, current_tab="/agent/auto-sell")
    ctx["rows"] = rows
    return templates.TemplateResponse("partials/auto_sell_rows.html", ctx)


@router.get("/history")
async def agent_history(
    user: User = Depends(get_current_user),
):
    """Permanent redirect: Phase-7 ``/agent/trades`` replaces the old placeholder.

    Kept so any bookmarks or in-flight links to ``/agent/history`` still land
    on the live trades page instead of 404'ing. We still gate on role first —
    a redirect that fires before auth would let an unauthenticated user infer
    the URL exists.
    """
    _require_agent_or_admin(user)
    return Response(
        status_code=status.HTTP_308_PERMANENT_REDIRECT,
        headers={"Location": "/agent/trades"},
    )


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
    q: Optional[str] = None,
):
    """List the current agent's customers. Admin sees all; agents see only theirs.

    ``status`` and ``q`` (free-text search over display_name+username) are
    both bookmarkable query params.
    """
    _require_agent_or_admin(user)
    status_filter = status if status in {"pending", "assigned", "active"} else None
    q = q or None
    if user.role == "admin":
        customers = await services_customers.list_customers(
            db, status=status_filter, q=q
        )
    else:
        customers = await services_customers.list_customers(
            db, agent_id=user.id, status=status_filter, q=q
        )
    trade_counts = await services_customers.get_customer_trade_counts(
        db, [c.id for c in customers]
    )
    # True total of THIS agent's trade instructions (unfiltered) — powers the
    # "delete all" danger-zone button, which is itself scoped to user.id. We
    # scope to user.id even for admins so the count matches what the button
    # would actually delete (the admin's own accounts, normally none).
    from sqlalchemy import func as _sa_func
    from app.models.trade_instructions import TradeInstruction
    total_trade_instructions = (
        await db.execute(
            select(_sa_func.count())
            .select_from(TradeInstruction)
            .join(Customer, Customer.id == TradeInstruction.customer_id)
            .where(Customer.agent_id == user.id)
        )
    ).scalar() or 0
    servers_by_id = {s.id: s for s in await services_servers.list_servers(db)}
    ctx = _ctx(request, user, current_tab="/agent/customers")
    ctx["customers"] = customers
    ctx["trade_counts"] = trade_counts
    ctx["total_trade_instructions"] = total_trade_instructions
    ctx["servers_by_id"] = servers_by_id
    ctx["filter_status"] = status_filter
    ctx["filter_q"] = q or ""
    return templates.TemplateResponse("agent/customers.html", ctx)


@router.get("/customers/new")
async def agent_customer_new(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the "add customer" form with empty values."""
    _require_agent_or_admin(user)
    ctx = _ctx(request, user, current_tab="/agent/customers")
    ctx["form_error"] = None
    ctx["form_values"] = {}
    ctx["broker_groups"] = await brokers_admin.list_enabled_grouped(db)
    ctx["mode"] = "create"
    return templates.TemplateResponse("agent/customer_form.html", ctx)


@router.post("/customers")
async def agent_customer_create(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    display_name: str = Form(...),
    broker: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
):
    """Create a new account-shaped customer owned by the current agent.

    Post-migration 0003, the form is account-shaped. Trade instructions
    are added via the per-customer detail page.
    """
    _require_agent_or_admin(user)

    sticky = {
        "display_name": display_name,
        "broker": broker,
        "username": username,
    }

    try:
        payload = CustomerCreate(
            display_name=display_name,
            broker=broker,  # type: ignore[arg-type]
            username=username,
            password=password,
        )
    except (ValidationError, ValueError) as exc:
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["form_error"] = (
            "Invalid input. Please review the form fields and try again."
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        ctx["form_values"] = sticky
        ctx["broker_groups"] = await brokers_admin.list_enabled_grouped(db)
        ctx["mode"] = "create"
        return templates.TemplateResponse(
            "agent/customer_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        customer = await services_customers.create_customer(
            db, agent_id=user.id, data=payload, actor_id=user.id
        )
    except ValueError as exc:
        await db.refresh(user)
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["form_error"] = str(exc)
        ctx["form_values"] = sticky
        ctx["broker_groups"] = await brokers_admin.list_enabled_grouped(db)
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
    """Render the per-customer detail page + trade-instruction list.

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
    trade_instructions = await services_trade_instructions.list_trade_instructions(
        db, customer_id
    )
    ctx = _ctx(request, user, current_tab="/agent/customers")
    ctx["customer"] = customer
    ctx["server"] = server
    ctx["trade_instructions"] = trade_instructions
    return templates.TemplateResponse("agent/customer_detail.html", ctx)


@router.get("/customers/{customer_id}/edit")
async def agent_customer_edit_form(
    customer_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the "edit customer" form (account-shaped fields only).

    Password intentionally NOT pre-filled — agent leaves empty to keep
    the current password, or types a new one to rotate. Per-trade
    edits live on the customer detail page.
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
        "username": customer.username,
    }
    ctx["broker_groups"] = await brokers_admin.list_enabled_grouped(db)
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
    username: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    version: int = Form(...),
):
    """Apply a partial, optimistic-locked update to a Customer (account)."""
    _require_agent_or_admin(user)
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or not _can_access_customer(user, customer):
        raise HTTPException(status_code=404, detail="customer not found")

    update_kwargs: dict = {}
    if display_name is not None:
        update_kwargs["display_name"] = display_name
    if broker is not None:
        update_kwargs["broker"] = broker
    if username is not None:
        update_kwargs["username"] = username
    if password:
        update_kwargs["password"] = password

    # PR #73 pattern: snapshot the customer's mutable attrs to primitives
    # BEFORE handing the row to ``update_customer``. The service does
    # ``db.rollback()`` on the duplicate-tuple IntegrityError before
    # raising ValueError, which expires every loaded ORM attribute. Any
    # subsequent attribute access (e.g. in the Jinja-sync template) would
    # trigger a lazy-load via ``do_ping_w_event`` and explode with
    # ``MissingGreenlet``.
    _customer_snap = SimpleNamespace(
        id=customer.id,
        display_name=customer.display_name,
        broker=customer.broker,
        username=customer.username,
        version=customer.version,
    )

    sticky = {
        "display_name": display_name if display_name is not None else customer.display_name,
        "broker": broker if broker is not None else customer.broker,
        "username": username if username is not None else customer.username,
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
        ctx["broker_groups"] = await brokers_admin.list_enabled_grouped(db)
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
        fresh = await services_customers.get_customer(db, customer_id)
        if fresh is None or not _can_access_customer(user, fresh):
            raise HTTPException(status_code=404, detail="customer not found")
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["form_error"] = (
            "Another change won the race. Reload the page and try again."
        )
        ctx["form_values"] = sticky
        ctx["broker_groups"] = await brokers_admin.list_enabled_grouped(db)
        ctx["mode"] = "edit"
        ctx["customer"] = fresh
        return templates.TemplateResponse(
            "agent/customer_form.html",
            ctx,
            status_code=status.HTTP_409_CONFLICT,
        )
    except ValueError as exc:
        await db.refresh(user)
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["form_error"] = str(exc)
        ctx["form_values"] = sticky
        ctx["broker_groups"] = await brokers_admin.list_enabled_grouped(db)
        ctx["mode"] = "edit"
        # Use the snapshot taken before update_customer — the live ``customer``
        # object's attrs are expired after the rollback (see comment above).
        ctx["customer"] = _customer_snap
        return templates.TemplateResponse(
            "agent/customer_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await _push_customer_stack_config(db, customer_id, actor_id=user.id)

    redirect_to = f"/agent/customers/{customer_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


# Customer delete intentionally absent — see admin.py for the rationale.
# To stop trading for an account, delete each of its TradeInstructions.


# ---------------------------------------------------------------------------
# TradeInstruction CRUD (per-customer sub-resource)
# ---------------------------------------------------------------------------


@router.get("/customers/{customer_id}/trade-instructions/new")
async def agent_trade_instruction_new_form(
    customer_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the "+ Add trade" form for a customer."""
    _require_agent_or_admin(user)
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or not _can_access_customer(user, customer):
        raise HTTPException(status_code=404, detail="customer not found")
    ctx = _ctx(request, user, current_tab="/agent/customers")
    ctx["customer"] = customer
    ctx["form_error"] = None
    ctx["form_values"] = {}
    ctx["mode"] = "create"
    return templates.TemplateResponse("agent/trade_instruction_form.html", ctx)


@router.post("/customers/{customer_id}/trade-instructions")
async def agent_trade_instruction_create(
    customer_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    isin: str = Form(...),
    side: int = Form(...),
    comment: Optional[str] = Form(None),
    auto_sell_threshold: Optional[str] = Form(None),
):
    """Create a new TradeInstruction under a customer the agent owns."""
    _require_agent_or_admin(user)
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or not _can_access_customer(user, customer):
        raise HTTPException(status_code=404, detail="customer not found")

    # PR #73 pattern: snapshot the parent customer's attrs BEFORE the
    # service call so the error renderer can use plain primitives even
    # after the rollback expires the live ORM row.
    _customer_snap = SimpleNamespace(
        id=customer.id,
        display_name=customer.display_name,
        broker=customer.broker,
        username=customer.username,
    )

    sticky = {
        "isin": isin,
        "side": str(side),
        "comment": comment or "",
        "auto_sell_threshold": auto_sell_threshold or "",
    }

    try:
        # Form-layer alias: side=3 ("Auto-sell only") maps to a side=1 row
        # flagged watch-only. ``sticky`` above keeps the RAW posted value so
        # the third radio stays selected on a validation re-render.
        mapped_side, auto_sell_only = map_side_form(side)
        payload = TradeInstructionCreate(
            isin=isin,
            side=mapped_side,  # type: ignore[arg-type]
            comment=comment if comment else None,
            auto_sell_threshold=_parse_optional_int(auto_sell_threshold),
            auto_sell_only=auto_sell_only,
        )
    except (ValidationError, ValueError) as exc:
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["customer"] = _customer_snap
        ctx["form_error"] = (
            "Invalid input. Please review the form fields and try again."
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        ctx["form_values"] = sticky
        ctx["mode"] = "create"
        return templates.TemplateResponse(
            "agent/trade_instruction_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        await services_trade_instructions.create_trade_instruction(
            db, customer_id, payload, actor_id=user.id
        )
    except ValueError as exc:
        await db.refresh(user)
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["customer"] = _customer_snap
        ctx["form_error"] = str(exc)
        ctx["form_values"] = sticky
        ctx["mode"] = "create"
        return templates.TemplateResponse(
            "agent/trade_instruction_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Push the new config.ini so the trading host picks up the new trade
    # instruction without waiting for an unrelated mutation. Best-effort:
    # SSH errors are logged but don't fail the redirect — the DB write
    # has already committed.
    await _push_customer_stack_config(db, customer_id, actor_id=user.id)

    redirect_to = f"/agent/customers/{customer_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


@router.get("/customers/{customer_id}/trade-instructions/{trade_id}/edit")
async def agent_trade_instruction_edit_form(
    customer_id: UUID,
    trade_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_agent_or_admin(user)
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or not _can_access_customer(user, customer):
        raise HTTPException(status_code=404, detail="customer not found")
    ti = await services_trade_instructions.get_trade_instruction(db, trade_id)
    if ti is None or ti.customer_id != customer_id:
        raise HTTPException(status_code=404, detail="trade not found")

    ctx = _ctx(request, user, current_tab="/agent/customers")
    ctx["customer"] = customer
    ctx["trade_instruction"] = ti
    ctx["form_error"] = None
    ctx["form_values"] = {
        "isin": ti.isin,
        # A watch-only row is stored side=1 + auto_sell_only; the form's
        # third radio is its alias, so pre-select "3" for it.
        "side": "3" if ti.auto_sell_only else str(ti.side),
        "comment": ti.comment or "",
        "auto_sell_threshold": ti.auto_sell_threshold if ti.auto_sell_threshold is not None else "",
        "version": ti.version,
    }
    ctx["mode"] = "edit"
    return templates.TemplateResponse("agent/trade_instruction_form.html", ctx)


@router.post("/customers/{customer_id}/trade-instructions/{trade_id}/edit")
async def agent_trade_instruction_update(
    customer_id: UUID,
    trade_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    isin: Optional[str] = Form(None),
    side: Optional[int] = Form(None),
    comment: Optional[str] = Form(None),
    auto_sell_threshold: Optional[str] = Form(None),
    version: int = Form(...),
):
    _require_agent_or_admin(user)
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or not _can_access_customer(user, customer):
        raise HTTPException(status_code=404, detail="customer not found")
    ti = await services_trade_instructions.get_trade_instruction(db, trade_id)
    if ti is None or ti.customer_id != customer_id:
        raise HTTPException(status_code=404, detail="trade not found")

    fields: dict = {"version": version}
    if isin is not None and isin != "":
        fields["isin"] = isin
    # ``side`` is mapped (form alias 3 → side=1 + auto_sell_only) inside the
    # payload try below so an invalid value lands in the same error re-render
    # path as a ValidationError.
    if comment is not None:
        fields["comment"] = comment if comment != "" else None
    # Always include it (form submits empty when unset / on a Sell) so the
    # operator can clear an existing threshold. ``0`` → None (disabled).
    if auto_sell_threshold is not None:
        fields["auto_sell_threshold"] = _parse_optional_int(auto_sell_threshold) or None

    # PR #73 pattern: snapshot both the TI and the parent Customer to
    # primitives before the service call. After ``db.rollback()`` on a
    # duplicate-tuple IntegrityError, both ORM objects' attrs are expired
    # and the sync Jinja render would lazy-load → MissingGreenlet.
    _ti_snap = SimpleNamespace(
        id=ti.id,
        customer_id=ti.customer_id,
        isin=ti.isin,
        side=ti.side,
        comment=ti.comment,
        auto_sell_threshold=ti.auto_sell_threshold,
        auto_sell_only=ti.auto_sell_only,
        version=ti.version,
    )
    _customer_snap = SimpleNamespace(
        id=customer.id,
        display_name=customer.display_name,
        broker=customer.broker,
        username=customer.username,
    )

    sticky = {
        "isin": isin if isin is not None and isin != "" else ti.isin,
        # Sticky side prefers the RAW posted value (so the "3" alias radio
        # stays selected); the stored-row fallback re-derives the alias.
        "side": (
            str(side)
            if side is not None
            else ("3" if ti.auto_sell_only else str(ti.side))
        ),
        "comment": comment if comment is not None else (ti.comment or ""),
        "auto_sell_threshold": (
            auto_sell_threshold if auto_sell_threshold is not None
            else (ti.auto_sell_threshold if ti.auto_sell_threshold is not None else "")
        ),
        "version": ti.version,
    }

    try:
        if side is not None:
            # Form-layer alias: 3 → (1, True). The raw ``side`` stays
            # untouched for the sticky re-render above.
            fields["side"], fields["auto_sell_only"] = map_side_form(side)
        payload = TradeInstructionUpdate(**fields)
    except (ValidationError, ValueError) as exc:
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["customer"] = _customer_snap
        ctx["trade_instruction"] = _ti_snap
        ctx["form_error"] = (
            "Invalid input. Please review the form fields and try again."
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        ctx["form_values"] = sticky
        ctx["mode"] = "edit"
        return templates.TemplateResponse(
            "agent/trade_instruction_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        await services_trade_instructions.update_trade_instruction(
            db, trade_id, payload, actor_id=user.id
        )
    except TradeInstructionLockError:
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["customer"] = _customer_snap
        ctx["trade_instruction"] = _ti_snap
        ctx["form_error"] = (
            "Another change won the race. Reload the page and try again."
        )
        ctx["form_values"] = sticky
        ctx["mode"] = "edit"
        return templates.TemplateResponse(
            "agent/trade_instruction_form.html",
            ctx,
            status_code=status.HTTP_409_CONFLICT,
        )
    except ValueError as exc:
        await db.refresh(user)
        ctx = _ctx(request, user, current_tab="/agent/customers")
        ctx["customer"] = _customer_snap
        ctx["trade_instruction"] = _ti_snap
        ctx["form_error"] = str(exc)
        ctx["form_values"] = sticky
        ctx["mode"] = "edit"
        return templates.TemplateResponse(
            "agent/trade_instruction_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    await _push_customer_stack_config(db, customer_id, actor_id=user.id)

    redirect_to = f"/agent/customers/{customer_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


@router.post("/customers/{customer_id}/trade-instructions/{trade_id}/delete")
async def agent_trade_instruction_delete(
    customer_id: UUID,
    trade_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete a TradeInstruction and push the new ``config.ini``."""
    _require_agent_or_admin(user)
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or not _can_access_customer(user, customer):
        raise HTTPException(status_code=404, detail="customer not found")
    ti = await services_trade_instructions.get_trade_instruction(db, trade_id)
    if ti is None or ti.customer_id != customer_id:
        raise HTTPException(status_code=404, detail="trade not found")
    await services_trade_instructions.hard_delete_trade_instruction(
        db, trade_id, actor_id=user.id
    )
    await _push_customer_stack_config(db, customer_id, actor_id=user.id)
    redirect_to = f"/agent/customers/{customer_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


@router.post("/trade-instructions/delete-all")
async def agent_trade_instructions_delete_all(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete EVERY trade instruction across ALL of the current agent's
    customers, then re-push each affected stack's ``config.ini`` so the trading
    hosts drop the sections.

    Tenant-scoped to ``user.id`` — an agent can only clear their own customers'
    instructions (an admin acting here clears only the accounts they own, which
    is normally none). The accounts themselves are untouched.
    """
    _require_agent_or_admin(user)
    deleted, affected_stacks = await services_trade_instructions.delete_all_for_agent(
        db, user.id, actor_id=user.id
    )
    # Re-push config.ini for each affected stack (best-effort; the DB delete is
    # already committed, so an SSH failure just means the host converges on the
    # next push/redeploy).
    for stack_id in affected_stacks:
        try:
            await services_stacks.push_config_ini_for_stack(
                db, stack_id=stack_id, actor_id=user.id
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception(
                "delete-all: config.ini push failed stack=%s", stack_id
            )
    logger.info(
        "agent %s bulk-deleted %d trade instruction(s) across %d stack(s)",
        user.id, deleted, len(affected_stacks),
    )
    redirect_to = "/agent/customers"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


@router.get("/customers/{customer_id}/holdings")
async def agent_customer_holdings(
    customer_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    isin: str = "",
):
    """Live holdings probe for the trade-instruction form's "Auto-sell only"
    preview — the agent-scoped twin of ``admin_customer_holdings``.

    Ownership-scoped like every other ``/agent/customers/{cid}`` route (an
    agent probing another agent's customer UUID gets 404, not data). This is
    a live captcha→OCR→login call — it can take seconds and can fail; ANY
    failure returns a 200 with an ``error`` field (the form renders a muted
    hint, never a 500) and the real exception goes to the server log.
    """
    _require_agent_or_admin(user)
    customer = await services_customers.get_customer(db, customer_id)
    if customer is None or not _can_access_customer(user, customer):
        raise HTTPException(status_code=404, detail="customer not found")
    effective_isin = (isin or "").strip().upper()
    if not effective_isin:
        # A cleared/partially-typed ISIN field is normal form interaction,
        # not a broker failure — no exception log.
        return JSONResponse({"isin": "", "error": "no isin to probe"})
    try:
        password = await services_customers.decrypt_password(customer)
        ocr_service_url = await settings_store.get_setting(db, "ocr_service_url")
        holdings = await broker_client.get_holdings(
            customer.broker,
            customer.username,
            password,
            effective_isin,
            ocr_service_url=ocr_service_url,
        )
    except Exception:  # noqa: BLE001 — degrade to a muted hint, log the cause
        logger.exception(
            "holdings probe failed for customer %s isin %s",
            customer_id, effective_isin,
        )
        return JSONResponse(
            {"isin": effective_isin, "error": "could not fetch holding"}
        )
    return JSONResponse({"isin": effective_isin, "holdings": holdings})


@router.get("/instruments/search")
async def agent_instruments_search(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    q: str = "",
    limit: int = 20,
):
    """Typeahead source (name/symbol → ISIN) for the agent trade-instruction
    form, via the market-data sidecar. Market-data is public/market-wide, so no
    per-agent scoping is needed; we only gate on being a logged-in agent/admin."""
    _require_agent_or_admin(user)
    rows = await market_data_client.search_instruments(
        db, q, limit=min(max(limit, 1), 50)
    )
    return {"instruments": rows}


@router.get("/fees")
async def agent_fees(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read-only fee panel: what the agent owes the operator, per customer
    (owed − paid = remaining). Scoped to the agent's own customers; admins see
    the whole fleet. Recording payments stays admin-only (#116)."""
    _require_agent_or_admin(user)
    own_agent_id = None if user.role == "admin" else user.id

    def _parse_date(s):
        try:
            return date.fromisoformat((s or "").strip())
        except (ValueError, TypeError):
            return None

    def _parse_time(s):
        try:
            return time.fromisoformat((s or "").strip())
        except (ValueError, TypeError):
            return None

    # Same settings-backed defaults the admin bot-report uses, so the agent sees
    # the same numbers for their customers.
    since = _parse_date(await settings_store.get_setting(db, "robot_start_date"))
    ws = _parse_time(await settings_store.get_setting(db, "bot_window_start"))
    we = _parse_time(await settings_store.get_setting(db, "bot_window_end"))
    exclude = services_broker_orders.parse_exclusions(
        (await settings_store.get_setting(db, "excluded_instruments")) or ""
    )

    report = await services_profit_report.build_fee_report(
        db,
        agent_id=own_agent_id,
        since=since,
        window_start=ws,
        window_end=we,
        exclude=exclude,
    )

    cust_ids = [cid for cid in report.per_customer if cid is not None]
    customers_by_id: dict[UUID, Customer] = {}
    if cust_ids:
        res = await db.execute(select(Customer).where(Customer.id.in_(cust_ids)))
        customers_by_id = {c.id: c for c in res.scalars().all()}

    ctx = _ctx(request, user, current_tab="/agent/fees")
    ctx["fee_report"] = report
    ctx["customers_by_id"] = customers_by_id
    ctx["grand_paid"] = sum((t.paid for t in report.per_customer.values()), 0)
    ctx["grand_remaining"] = sum(
        (t.remaining for t in report.per_customer.values()), 0
    )
    return templates.TemplateResponse("agent/fees.html", ctx)


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
    except StackRunLockBusyError:
        # Browser users get redirected to the in-flight run's live log
        # instead of a raw 409 JSON. HTMX / JSON callers still get the 409
        # so they can decide how to handle it.
        in_flight = await db.execute(
            select(StackRunLock).where(StackRunLock.stack_id == stack.id)
        )
        lock_row = in_flight.scalar_one_or_none()
        if request.headers.get("HX-Request") or "application/json" in (
            request.headers.get("accept") or ""
        ):
            raise HTTPException(
                status_code=409,
                detail="another run is already in flight on this stack",
            )
        target = (
            f"/agent/runs/{lock_row.run_id}"
            if lock_row is not None
            else f"/agent/stacks/{stack.id}"
        )
        return Response(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": target},
        )

    redirect_to = f"/agent/runs/{run.id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": redirect_to})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": redirect_to},
    )


@router.post("/stacks/{stack_id}/redeploy")
async def agent_stack_redeploy(
    stack_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-render config + ``docker compose up -d --force-recreate`` on the
    agent's own stack.

    Useful when the agent has just saved a new scheduler time or locust
    config and wants the in-container scheduler to pick it up
    immediately instead of waiting for the next 1-second poll (the bot
    DOES poll the bind-mounted file every second, but the container
    has to be re-created if any volume mount's inode changed — which
    is rare for the in-place write we do, but harmless to force).

    Tenant scoping: 404 (not 403) if this isn't the agent's own stack.
    Deprovision is intentionally *not* exposed to agents — that
    destructive action stays admin-only.
    """
    _require_agent_or_admin(user)
    stack = await _load_stack_for_agent(db, user, stack_id)
    try:
        result = await services_stacks.redeploy_stack(
            db, stack.id, actor_id=user.id
        )
    except RuntimeError as exc:
        # Compose lock busy — surface as a 400 with the partial.
        from app.schemas.stack import StackActionResult
        result = StackActionResult(
            ok=False,
            stack_id=stack.id,
            status=stack.status,
            message=str(exc),
            log_tail="",
        )
    except SSHError as exc:
        # Same graceful-degrade as admin: render the action partial with
        # the error instead of bubbling a 500.
        from app.schemas.stack import StackActionResult
        result = StackActionResult(
            ok=False,
            stack_id=stack.id,
            status="down",
            message=f"redeploy failed: {exc}",
            log_tail=str(exc),
        )
    ctx = _ctx(request, user, current_tab="/agent/stacks")
    ctx["result"] = result
    # Re-use the admin partial — it's identical markup, no admin-only
    # bits leaked into it.
    return templates.TemplateResponse(
        "admin/partials/stack_action_result.html", ctx
    )


@router.post("/stacks/{stack_id}/force-kill")
async def agent_stack_force_kill(
    stack_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Force-stop the agent's own stack (``docker compose stop -t 0``).

    Immediate SIGKILL, scoped to this stack's compose project only. The row
    is flipped to ``down``; reversible via Redeploy. Tenant-guarded: 404 (not
    403) if this isn't the agent's own stack. Returns the action partial so
    HTMX can swap it into ``#stack-action-result`` (same UX as Redeploy).
    """
    _require_agent_or_admin(user)
    stack = await _load_stack_for_agent(db, user, stack_id)
    try:
        result = await services_stacks.force_stop_stack(
            db, stack.id, actor_id=user.id
        )
    except RuntimeError as exc:
        from app.schemas.stack import StackActionResult
        result = StackActionResult(
            ok=False,
            stack_id=stack.id,
            status=stack.status,
            message=str(exc),
            log_tail="",
        )
    except SSHError as exc:
        from app.schemas.stack import StackActionResult
        result = StackActionResult(
            ok=False,
            stack_id=stack.id,
            status="down",
            message=f"force-kill failed: {exc}",
            log_tail=str(exc),
        )
    ctx = _ctx(request, user, current_tab="/agent/stacks")
    ctx["result"] = result
    return templates.TemplateResponse(
        "admin/partials/stack_action_result.html", ctx
    )


@router.post("/stacks/force-kill-all")
async def agent_stacks_force_kill_all(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Force-stop all of the caller's stacks (agent: own; admin: all).

    Best-effort per stack; redirects back to the list with a result summary.
    """
    _require_agent_or_admin(user)
    targets = await _stacks_visible_to(db, user)
    results = await services_stacks.force_stop_stacks(
        db, [s.id for s in targets], actor_id=user.id
    )
    ok = sum(1 for r in results if r.ok)
    fail = len(results) - ok
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={
            "Location": _stacks_bulk_location(
                "/agent/stacks", bulk="fk", ok=ok, fail=fail
            )
        },
    )


@router.post("/stacks/run-all")
async def agent_stacks_run_all(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    job_name: str = Form(...),
):
    """Fire a manual run on all of the caller's stacks (agent: own; admin: all).

    A stack with a run already in flight is skipped (not failed). Redirects
    back to the list with a started/skipped/failed summary.
    """
    _require_agent_or_admin(user)
    if job_name not in ("cache_warmup", "run_trading"):
        raise HTTPException(
            status_code=400, detail=f"unknown job_name: {job_name}"
        )
    targets = await _stacks_visible_to(db, user)
    started, skipped, failed = await run_executor.run_all_stacks(
        targets, job_name=job_name, actor_id=user.id
    )
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={
            "Location": _stacks_bulk_location(
                "/agent/stacks", bulk="run",
                job=job_name, ok=started, skip=skipped, fail=failed,
            )
        },
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

    # Bulk-count EXECUTED trades per run (executed_volume>0) so a failed/
    # no-trade run is not shown as "partial · N" from placed-but-rejected
    # orders (executed_volume=0); see issue #107.
    from sqlalchemy import func as _sa_func
    from app.models.trades import TradeResult
    trade_counts_by_run: dict[UUID, int] = {}
    if runs:
        run_ids = [r.id for r in runs]
        rows = await db.execute(
            select(TradeResult.run_id, _sa_func.count(TradeResult.id))
            .where(TradeResult.run_id.in_(run_ids))
            .where(TradeResult.executed_volume > 0)
            .group_by(TradeResult.run_id)
        )
        trade_counts_by_run = {rid: cnt for rid, cnt in rows.all()}

    ctx = _ctx(request, user, current_tab="/agent/runs")
    ctx["runs"] = runs
    ctx["stacks_by_id"] = stacks_by_id
    ctx["trade_counts_by_run"] = trade_counts_by_run
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
    # (the WS handler will replay any already-buffered output). Only the
    # TAIL renders inline — the complete log is the log.txt download.
    archived_log = ""
    archived_log_total = 0
    archived_log_truncated = False
    if run.status != "running":
        tail, total = await services_runs.read_run_log_tail(run)
        archived_log = tail.decode("utf-8", errors="replace")
        archived_log_total = total
        archived_log_truncated = total > len(tail)

    ctx = _ctx(request, user, current_tab="/agent/runs")
    ctx["run"] = run
    ctx["stack"] = stack
    # NOTE: we pass ``agent`` (the stack owner's User row) so the template
    # can show the *username* — never the raw UUID, which would leak
    # tenant info to anyone over-the-shoulder.
    ctx["agent"] = agent_user
    ctx["archived_log"] = archived_log
    ctx["archived_log_total"] = archived_log_total
    ctx["archived_log_truncated"] = archived_log_truncated
    return templates.TemplateResponse("agent/run_detail.html", ctx)


@router.get("/runs/{run_id}/log.txt")
async def agent_run_log_download(
    run_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download the COMPLETE archived run log (tenant-guarded, 404-masked).

    Same serving strategy as the admin route: gzipped blobs go out with
    ``Content-Encoding: gzip`` and the browser inflates to full text.
    """
    _require_agent_or_admin(user)
    run = await services_runs.get_run(db, run_id)
    if run is None or not services_runs.can_user_see_run(user, run):
        raise HTTPException(status_code=404, detail="run not found")
    if not run.log_blob_ref or not Path(run.log_blob_ref).exists():
        raise HTTPException(status_code=404, detail="no archived log for this run")
    headers = {"Content-Disposition": f'attachment; filename="run-{run_id}.log"'}
    if run.log_blob_ref.endswith(".gz"):
        headers["Content-Encoding"] = "gzip"
    return FileResponse(
        run.log_blob_ref,
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


@router.post("/runs/{run_id}/terminate")
async def agent_run_terminate(
    run_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Agent cancels their own in-flight run (admin acting-as-agent allowed).

    Tenant-guarded: a non-admin agent can only terminate runs whose
    ``agent_id`` matches their user id — same 404-mask pattern as the
    rest of this module so an agent guessing UUIDs can't enumerate other
    tenants' runs.
    """
    _require_agent_or_admin(user)
    run = await services_runs.get_run(db, run_id)
    if run is None or not services_runs.can_user_see_run(user, run):
        raise HTTPException(status_code=404, detail="run not found")

    target = f"/agent/runs/{run_id}"
    if run.status != "running":
        if request.headers.get("HX-Request"):
            return Response(status_code=204, headers={"HX-Redirect": target})
        return Response(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": target})

    db.add(AuditLog(
        actor_user_id=user.id,
        action="run.terminate",
        target_type="run",
        target_id=str(run.id),
        before_json={"status": run.status},
        after_json={"status": "killed_pending"},
        ts=datetime.now(timezone.utc),
    ))
    await db.commit()

    cancelled = run_executor.terminate_run(run_id)
    logger.info(
        "agent %s terminated run %s (cancelled=%s)", user.username, run_id, cancelled
    )

    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": target})


# ---------------------------------------------------------------------------
# Trades (Phase 7)
# ---------------------------------------------------------------------------
#
# Read-only views over the ``trade_results`` table that the trade ingestor
# (parallel agent — NOT this router) writes to as it pulls JSON dumps off the
# trading bots. The list page mirrors the admin equivalent but drops the
# agent picker: an agent always sees ONLY their own customers' trades.
#
# Tenant scoping is enforced in two places:
#
# 1. List view (:func:`agent_trades`) passes ``agent_id=user.id`` into the
#    service layer when the caller is not an admin, so we never load other
#    tenants' rows into Python memory at all.
# 2. Detail view (:func:`agent_trade_detail`) re-looks up the customer for
#    the trade and gates on ``services_trades.can_user_see_trade`` — that
#    pure function returns False whenever ``customer.agent_id != user.id``
#    for non-admins. On False we surface a 404 (NOT 403) so an agent
#    guessing UUIDs cannot enumerate other tenants' trades.


@router.get("/trades")
async def agent_trades(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    customer_id: Optional[str] = None,
    broker: Optional[str] = None,
    symbol_or_isin: Optional[str] = None,
    state: Optional[str] = None,
    side: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    show_all: Optional[str] = None,
):
    """List the current agent's own trades (admin sees all).

    All filter query params are empty-string tolerant: blank or unparseable
    values are coerced to ``None`` (= "no filter") rather than 422-ing, so
    the filter form can submit an empty ``?state=`` for "any state". The
    UUID + datetime + int parsers below match the pattern used by the
    agent_runs route.
    """
    _require_agent_or_admin(user)
    from datetime import datetime

    def _parse_date_or_none(s: Optional[str]):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    def _parse_int_or_none(s: Optional[str]):
        if not s:
            return None
        try:
            return int(s)
        except (ValueError, TypeError):
            return None

    def _parse_uuid_or_none(s: Optional[str]):
        if not s:
            return None
        try:
            return UUID(s)
        except (ValueError, TypeError):
            return None

    # Critical tenant scope: agents pass their own user id; admin passes
    # None so the service-layer query doesn't filter by agent at all. This
    # is the SAME shape as agent_runs and the customers list — keep it.
    own_agent_id = None if user.role == "admin" else user.id

    # Default to executed trades only; "Show all" reveals placed-but-rejected
    # (executed_volume=0) orders for forensics (#107).
    executed_only = not bool(show_all)
    trades = await services_trades.list_trades(
        db,
        agent_id=own_agent_id,
        customer_id=_parse_uuid_or_none(customer_id),
        broker=broker or None,
        symbol_or_isin=symbol_or_isin or None,
        state=_parse_int_or_none(state),
        side=_parse_int_or_none(side),
        since=_parse_date_or_none(since),
        until=_parse_date_or_none(until),
        executed_only=executed_only,
        limit=500,
    )

    # Lookup dict for the customer column. We load JUST the customers that
    # appear in the result set (vs. every customer the agent owns) — this
    # keeps the dict small when the trades list is narrow. Tenant-safety:
    # the trades list is already agent-scoped, so the customer_ids we look
    # up here are guaranteed to belong to ``user`` (or all, for admin).
    customers_by_id: dict[UUID, Customer] = {}
    if trades:
        cust_ids = list({t.customer_id for t in trades})
        result = await db.execute(
            select(Customer).where(Customer.id.in_(cust_ids))
        )
        for c in result.scalars().all():
            customers_by_id[c.id] = c

    # Customer picker source: only this agent's customers (admins see all).
    # We deliberately use a separate query rather than reusing the dict
    # above, because the dict only contains customers that already have a
    # trade — the picker should also list customers with zero trades yet.
    picker_customers = await services_customers.list_customers(
        db,
        agent_id=user.id if user.role != "admin" else None,
    )

    ctx = _ctx(request, user, current_tab="/agent/trades")
    ctx["trades"] = trades
    ctx["customers_by_id"] = customers_by_id
    ctx["picker_customers"] = sorted(
        picker_customers, key=lambda c: (c.display_name or "").lower()
    )
    ctx["filter_customer_id"] = customer_id or ""
    ctx["filter_broker"] = broker or ""
    ctx["filter_symbol_or_isin"] = symbol_or_isin or ""
    ctx["filter_state"] = state or ""
    ctx["filter_side"] = side or ""
    ctx["filter_since"] = since or ""
    ctx["filter_until"] = until or ""
    ctx["filter_show_all"] = bool(show_all)
    return templates.TemplateResponse("agent/trades.html", ctx)


@router.get("/trades/{trade_id}")
async def agent_trade_detail(
    trade_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-trade detail page.

    Tenant-guarded: we look up the customer for the trade and gate on
    :func:`services_trades.can_user_see_trade`. On miss / cross-tenant we
    surface 404 (NOT 403) so an agent who guesses another tenant's trade
    UUID cannot tell whether it exists.
    """
    _require_agent_or_admin(user)
    trade = await services_trades.get_trade(db, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="trade not found")
    customer = await services_customers.get_customer(db, trade.customer_id)
    # Tenant check:
    # - Admins can always view, even if the customer row has been deleted
    #   or the trade was ingested with a NULL/unmatched customer_id (which
    #   the list view surfaces — denying detail access would break the
    #   admin's only way to triage those rows).
    # - Agents must own the customer; ``can_user_see_trade`` returns False
    #   when the customer's agent_id != user.id, OR when customer is None
    #   (an agent can never see an unmatched trade).
    # - 404 not 403 on miss so an agent enumerating UUIDs can't tell
    #   whether the trade exists.
    if customer is None:
        if user.role != "admin":
            raise HTTPException(status_code=404, detail="trade not found")
    elif not services_trades.can_user_see_trade(user, trade, customer):
        raise HTTPException(status_code=404, detail="trade not found")

    run = None
    if trade.run_id:
        run = await services_runs.get_run(db, trade.run_id)

    import json as _json
    raw_pretty = _json.dumps(
        trade.raw_json or {}, indent=2, ensure_ascii=False
    )

    ctx = _ctx(request, user, current_tab="/agent/trades")
    ctx["trade"] = trade
    ctx["customer"] = customer
    ctx["run"] = run
    ctx["raw_pretty"] = raw_pretty
    return templates.TemplateResponse("agent/trade_detail.html", ctx)


# ---------------------------------------------------------------------------
# Health signals (Phase 8)
# ---------------------------------------------------------------------------
#
# Read-only list scoped to the current agent's stacks. Admins acting-as-agent
# see the whole fleet, same convention as /agent/runs and /agent/trades.
#
# Agents cannot ack signals here — only admins can, via /admin/health/<id>/ack.
# Keeping the ack action admin-only preserves a clean audit trail (one
# operator role doing the triage) and matches the broader "destructive /
# state-changing actions stay admin-only" pattern in this module.
#
# Cross-tenant safety: we load the agent's own stacks first, build a set
# of allowed stack ids, then post-filter the signals list. An agent who
# hand-edits a stack_id query param that belongs to someone else gets
# silently dropped (NOT 404'd) so we don't leak the existence of other
# tenants' stacks.


@router.get("/health")
async def agent_health(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    stack_id: Optional[str] = None,
    severity: Optional[str] = None,
    acked: Optional[str] = None,
):
    """Agent's own stacks' health signals.

    Admins see the whole fleet (same convention as /agent/runs etc).
    Filtering and signal listing match the admin route's contract. The
    ``stack_id`` filter is silently dropped if it refers to a stack the
    agent does not own — we never 404 on cross-tenant UUIDs because that
    would let an agent probe the UUID space.
    """
    _require_agent_or_admin(user)
    all_stacks = await services_stacks.list_stacks(db)
    my_stacks = (
        all_stacks
        if user.role == "admin"
        else [s for s in all_stacks if s.agent_id == user.id]
    )
    my_stack_ids = {s.id for s in my_stacks}

    def _uuid_or_none(s):
        if not s:
            return None
        try:
            return UUID(s)
        except (ValueError, TypeError):
            return None

    filter_stack = _uuid_or_none(stack_id)
    if filter_stack is not None and filter_stack not in my_stack_ids:
        # Silently drop a stack filter the agent doesn't own. NOT 404 —
        # we don't want to leak whether *some other* agent's stack id is
        # valid.
        filter_stack = None

    sev_filter = (
        severity if severity in ("info", "warning", "error", "critical") else None
    )
    acked_filter: Optional[bool] = None
    if acked == "yes":
        acked_filter = True
    elif acked == "no":
        acked_filter = False

    # Tenant scope pushed into the SQL: pass stack_ids when the caller
    # is an agent and they haven't already narrowed to a single stack
    # they own. This guarantees their own rows can't be displaced by a
    # noisy neighbouring stack inside the 300-row LIMIT window — the
    # previous post-filter could silently drop signals an agent should
    # have seen.
    sql_stack_ids: Optional[set[UUID]] = None
    if filter_stack is None and user.role != "admin":
        sql_stack_ids = my_stack_ids
    rows = await services_health.list_signals(
        db,
        stack_id=filter_stack,
        stack_ids=sql_stack_ids,
        severity=sev_filter,
        acked=acked_filter,
        limit=300,
    )

    # Pre-load ack'er usernames so the row template can render them
    # without lazy-loading per row.
    ack_user_ids = {r.ack_by for r in rows if r.ack_by}
    users_by_id: dict[UUID, "User"] = {}
    if ack_user_ids:
        result = await db.execute(
            select(User).where(User.id.in_(ack_user_ids))
        )
        for u in result.scalars().all():
            users_by_id[u.id] = u

    ctx = _ctx(request, user, current_tab="/agent/health")
    ctx["signals"] = rows
    ctx["stacks_by_id"] = {s.id: s for s in my_stacks}
    ctx["users_by_id"] = users_by_id
    ctx["all_stacks"] = sorted(my_stacks, key=lambda s: s.compose_project)
    ctx["filter_stack_id"] = stack_id or ""
    ctx["filter_severity"] = severity or ""
    ctx["filter_acked"] = acked or ""
    return templates.TemplateResponse("agent/health.html", ctx)


# Silence unused-import lint for AgentStack + select — referenced indirectly
# via services_stacks; kept here for forward use by an "agent stack detail"
# page in a later phase.
_ = AgentStack
_ = select
