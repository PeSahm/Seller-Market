"""Admin-only HTML routes for the UI-managed ``brokers`` table.

Mirrors the patterns in :mod:`app.routers.admin`:

* admin gate via ``Depends(require_admin)``
* the shared Jinja ``templates`` engine + ``_ctx`` context helper from
  :mod:`app.routers.dashboard`
* ``_flash_redirect``-style 303 / ``HX-Redirect`` responses
* CSRF via ``partials/csrf.html`` (a hidden ``csrf_token`` field rendered into
  every ``<form method="post">``)

The business logic — duplicate-code / in-use guards, audit rows, family-cache
re-warming — lives in :mod:`app.services.brokers_admin`; the handlers here only
parse forms, validate shape, and pick the template.
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
from app.models.users import User
from app.routers.dashboard import _ctx, templates
from app.schemas.broker import BrokerCreate, BrokerUpdate
from app.security.deps import require_admin
from app.services import brokers_admin as services_brokers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-ui"], include_in_schema=False)

_TAB = "/admin/brokers"


def _flash_redirect(request: Request, location: str) -> Response:
    """303 / ``HX-Redirect`` pattern (matches the customer routes)."""
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": location})
    return Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": location},
    )


@router.get("/brokers")
async def admin_brokers(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all brokers (enabled + disabled)."""
    brokers = await services_brokers.list_brokers(db, include_disabled=True)
    ctx = _ctx(request, user, current_tab=_TAB)
    ctx["brokers"] = brokers
    return templates.TemplateResponse("admin/brokers.html", ctx)


@router.get("/brokers/new")
async def admin_broker_new(
    request: Request,
    user: User = Depends(require_admin),
):
    """Render the "add broker" form."""
    ctx = _ctx(request, user, current_tab=_TAB)
    ctx["form_error"] = None
    ctx["form_values"] = {}
    ctx["mode"] = "create"
    return templates.TemplateResponse("admin/broker_form.html", ctx)


@router.post("/brokers")
async def admin_broker_create(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    code: str = Form(...),
    family: str = Form(...),
    label: str = Form(...),
    enabled: Optional[str] = Form(None),
    sort_order: int = Form(0),
    base_domain: Optional[str] = Form(None),
):
    """Create a broker. On validation/dup error, re-render the form."""
    # Unchecked HTML checkboxes are simply absent from the form body, so a
    # present ``enabled`` field (any value) means "checked".
    enabled_bool = enabled is not None
    sticky = {
        "code": code,
        "family": family,
        "label": label,
        "enabled": enabled_bool,
        "sort_order": sort_order,
        "base_domain": base_domain,
    }

    def _rerender(message: str) -> Response:
        ctx = _ctx(request, user, current_tab=_TAB)
        ctx["form_error"] = message
        ctx["form_values"] = sticky
        ctx["mode"] = "create"
        return templates.TemplateResponse(
            "admin/broker_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        payload = BrokerCreate(
            code=code,
            family=family,  # type: ignore[arg-type]
            label=label,
            enabled=enabled_bool,
            sort_order=sort_order,
            base_domain=base_domain,
        )
    except ValidationError:
        return _rerender(
            "Invalid input. Please review the form fields and try again."
        )

    try:
        broker = await services_brokers.create_broker(
            db, payload, actor_id=user.id
        )
    except ValueError as exc:
        # ``create_broker`` now ``db.rollback()``s on the duplicate-code race
        # before re-raising as ValueError. The rollback expires every loaded
        # attribute on the session, including ``user`` — and the shared
        # ``page_shell.html`` then touches ``current_user.role`` /
        # ``current_user.username``. Refresh it so the (sync) Jinja render
        # doesn't lazy-load and explode with ``MissingGreenlet`` (see PR #73).
        await db.refresh(user)
        return _rerender(str(exc))

    return _flash_redirect(request, f"/admin/brokers/{broker.id}/edit")


@router.get("/brokers/{broker_id}/edit")
async def admin_broker_edit(
    broker_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Render the edit form for an existing broker."""
    broker = await services_brokers.get_broker(db, broker_id)
    if broker is None:
        raise HTTPException(status_code=404, detail="broker not found")

    ctx = _ctx(request, user, current_tab=_TAB)
    ctx["form_error"] = None
    ctx["form_values"] = {
        "code": broker.code,
        "family": broker.family,
        "label": broker.label,
        "enabled": broker.enabled,
        "sort_order": broker.sort_order,
        "base_domain": broker.base_domain or "",
    }
    ctx["broker"] = broker
    ctx["mode"] = "edit"
    return templates.TemplateResponse("admin/broker_form.html", ctx)


@router.post("/brokers/{broker_id}")
async def admin_broker_update(
    broker_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    family: str = Form(...),
    label: str = Form(...),
    enabled: Optional[str] = Form(None),
    sort_order: int = Form(0),
    base_domain: Optional[str] = Form(None),
):
    """Update a broker's mutable fields. ``code`` is immutable (not accepted)."""
    enabled_bool = enabled is not None
    broker = await services_brokers.get_broker(db, broker_id)
    if broker is None:
        raise HTTPException(status_code=404, detail="broker not found")

    sticky = {
        "code": broker.code,  # immutable; shown read-only
        "family": family,
        "label": label,
        "enabled": enabled_bool,
        "sort_order": sort_order,
        "base_domain": base_domain,
    }

    def _rerender(message: str) -> Response:
        ctx = _ctx(request, user, current_tab=_TAB)
        ctx["form_error"] = message
        ctx["form_values"] = sticky
        ctx["broker"] = broker
        ctx["mode"] = "edit"
        return templates.TemplateResponse(
            "admin/broker_form.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        payload = BrokerUpdate(
            family=family,  # type: ignore[arg-type]
            label=label,
            enabled=enabled_bool,
            sort_order=sort_order,
            base_domain=base_domain,
        )
    except ValidationError:
        return _rerender(
            "Invalid input. Please review the form fields and try again."
        )

    try:
        await services_brokers.update_broker(
            db, broker_id, payload, actor_id=user.id
        )
    except ValueError as exc:
        # The service may rollback on error; refresh ``user`` so the shared
        # page_shell.html doesn't lazy-load ``current_user.role`` and explode
        # with MissingGreenlet in the sync Jinja render (see PR #73).
        await db.refresh(user)
        return _rerender(str(exc))

    return _flash_redirect(request, "/admin/brokers")


@router.post("/brokers/{broker_id}/toggle")
async def admin_broker_toggle(
    broker_id: UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Flip a broker's ``enabled`` flag.

    On an in-use guard failure we re-render the list with an error flash so the
    operator sees why the toggle was refused.
    """
    broker = await services_brokers.get_broker(db, broker_id)
    if broker is None:
        raise HTTPException(status_code=404, detail="broker not found")

    try:
        await services_brokers.set_enabled(
            db, broker_id, not broker.enabled, actor_id=user.id
        )
    except ValueError as exc:
        await db.refresh(user)
        brokers = await services_brokers.list_brokers(db, include_disabled=True)
        ctx = _ctx(request, user, current_tab=_TAB)
        ctx["brokers"] = brokers
        ctx["list_error"] = str(exc)
        return templates.TemplateResponse(
            "admin/brokers.html",
            ctx,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return _flash_redirect(request, "/admin/brokers")
