"""CRUD orchestration for the UI-managed ``brokers`` table.

The router stays thin: it converts a form into a :class:`BrokerCreate` /
:class:`BrokerUpdate` pydantic model, calls one function here, and renders the
result. This module owns the DB writes, the duplicate-code / in-use guards, the
audit-log rows, and (critically) re-warming the process-cached ``{code:
family}`` map after every mutation so the hot request/worker paths see the new
broker immediately.

In-use semantics
----------------
``customers.broker`` references a broker *by value* (no FK). Post-migration
0004 :class:`~app.models.customers.Customer` no longer has an ``enabled``
column, so we cannot distinguish an "enabled customer" from a disabled one.
We therefore keep it simple and safe: **any** customer that references a
broker's code blocks both disabling and deleting that broker. The operator must
re-point or remove those customers first.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.brokers import Broker
from app.models.customers import Customer
from app.schemas.broker import BrokerCreate, BrokerUpdate

logger = logging.getLogger(__name__)

# Family display order for the grouped dropdown helper.
_FAMILY_ORDER = ["ephoenix", "exir", "onlineplus"]


def _record_audit(
    db: AsyncSession,
    *,
    actor_id: Optional[UUID],
    action: str,
    target_id: UUID,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
) -> None:
    """Insert a single ``audit_log`` row for a broker mutation."""
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action=action,
            target_type="broker",
            target_id=str(target_id),
            before_json=before,
            after_json=after,
        )
    )


def _snapshot(broker: Broker) -> dict:
    """Audit-safe dict of a Broker row (nothing secret here)."""
    return {
        "id": str(broker.id),
        "code": broker.code,
        "family": broker.family,
        "label": broker.label,
        "enabled": broker.enabled,
        "sort_order": broker.sort_order,
        "base_domain": broker.base_domain,
    }


async def _warm_cache(db: AsyncSession) -> None:
    """Re-warm the family cache after a mutation (best-effort, never fatal)."""
    from app.services.brokers.registry import warm_family_cache

    try:
        await warm_family_cache(db)
    except Exception:  # noqa: BLE001 — cache warm must not fail the write
        logger.exception("failed to re-warm broker family cache after mutation")


async def _count_customers_for_code(db: AsyncSession, code: str) -> int:
    """How many customers reference this broker code (any state).

    Case-insensitive to match how a customer's broker is resolved elsewhere
    (``get_broker_by_code`` lowercases its lookup), so a mixed-case stored value
    can't slip past the in-use guard and let a referenced broker be
    disabled/deleted. ``code`` is already the lowercase ``broker.code``.
    """
    result = await db.execute(
        select(func.count())
        .select_from(Customer)
        .where(func.lower(Customer.broker) == code)
    )
    return int(result.scalar_one() or 0)


async def list_brokers(
    db: AsyncSession, *, include_disabled: bool = True
) -> list[Broker]:
    """All brokers ordered by ``(family, sort_order, label)``.

    ``include_disabled=False`` drops disabled rows (e.g. for dropdowns).
    """
    stmt = select(Broker)
    if not include_disabled:
        stmt = stmt.where(Broker.enabled.is_(True))
    stmt = stmt.order_by(Broker.family, Broker.sort_order, Broker.label)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_broker(db: AsyncSession, broker_id) -> Optional[Broker]:
    """Fetch a broker by id, or ``None``."""
    return await db.get(Broker, broker_id)


async def get_broker_by_code(db: AsyncSession, code: str) -> Optional[Broker]:
    """Fetch a broker by its (normalized) code, or ``None``."""
    normalized = (code or "").strip().lower()
    result = await db.execute(select(Broker).where(Broker.code == normalized))
    return result.scalar_one_or_none()


async def create_broker(
    db: AsyncSession, data: BrokerCreate, *, actor_id: Optional[UUID] = None
) -> Broker:
    """Create a broker. Duplicate ``code`` raises ``ValueError``.

    ``data.code`` is already lowercased/trimmed by the schema validator.
    """
    existing = await get_broker_by_code(db, data.code)
    if existing is not None:
        raise ValueError("broker code already exists")

    broker = Broker(
        code=data.code,
        family=data.family,
        label=data.label,
        enabled=data.enabled,
        sort_order=data.sort_order,
        base_domain=data.base_domain,
    )
    db.add(broker)
    # The ``existing is None`` pre-check above narrows the duplicate window but
    # cannot close it: two concurrent creates can both pass it, then race to the
    # ``code`` UNIQUE. Catch that IntegrityError and surface the same advertised
    # ``ValueError`` instead of leaking a raw DB error to the router.
    try:
        await db.flush()
        _record_audit(
            db,
            actor_id=actor_id,
            action="broker.create",
            target_id=broker.id,
            before=None,
            after=_snapshot(broker),
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError("broker code already exists") from exc

    await db.refresh(broker)
    await _warm_cache(db)
    return broker


async def update_broker(
    db: AsyncSession,
    broker_id,
    data: BrokerUpdate,
    *,
    actor_id: Optional[UUID] = None,
) -> Broker:
    """Update a broker's mutable fields (label / family / enabled / sort_order).

    ``code`` is immutable (customers reference it by value), so it is not a
    field on :class:`BrokerUpdate`. Not found raises ``ValueError``.
    """
    broker = await db.get(Broker, broker_id)
    if broker is None:
        raise ValueError("broker not found")

    before = _snapshot(broker)

    # If disabling via update, apply the same in-use guard as set_enabled.
    if data.enabled is False and broker.enabled:
        in_use = await _count_customers_for_code(db, broker.code)
        if in_use:
            raise ValueError(
                f"broker in use by {in_use} customers — cannot disable"
            )

    if data.label is not None:
        broker.label = data.label
    if data.family is not None and data.family != broker.family:
        # ``family`` is the ONLY field that selects the adapter / URL shape /
        # auth flow (resolved live via family_of(code) on every call). Flipping
        # it for an in-use broker would silently reroute all its customers to the
        # other broker's wire protocol and break them — guard it like disable.
        in_use = await _count_customers_for_code(db, broker.code)
        if in_use:
            raise ValueError(
                f"broker in use by {in_use} customers — cannot change family"
            )
        broker.family = data.family
    if data.enabled is not None:
        broker.enabled = data.enabled
    if data.sort_order is not None:
        broker.sort_order = data.sort_order
    # The broker form always submits ``base_domain`` (empty -> None via the
    # schema validator), so set it unconditionally — this is how an operator
    # CLEARS the domain (reverting an OnlinePlus broker to the code convention).
    broker.base_domain = data.base_domain

    _record_audit(
        db,
        actor_id=actor_id,
        action="broker.update",
        target_id=broker.id,
        before=before,
        after=_snapshot(broker),
    )
    await db.commit()
    await db.refresh(broker)
    await _warm_cache(db)
    return broker


async def set_enabled(
    db: AsyncSession,
    broker_id,
    enabled: bool,
    *,
    actor_id: Optional[UUID] = None,
) -> Broker:
    """Toggle a broker's ``enabled`` flag.

    Disabling a broker that is referenced by ANY customer raises ``ValueError``
    (see module docstring — Customer has no ``enabled`` column post-0004, so we
    treat any reference as "in use").
    """
    broker = await db.get(Broker, broker_id)
    if broker is None:
        raise ValueError("broker not found")

    if not enabled and broker.enabled:
        in_use = await _count_customers_for_code(db, broker.code)
        if in_use:
            raise ValueError(
                f"broker in use by {in_use} customers — cannot disable"
            )

    before = _snapshot(broker)
    broker.enabled = enabled
    _record_audit(
        db,
        actor_id=actor_id,
        action="broker.set_enabled",
        target_id=broker.id,
        before=before,
        after=_snapshot(broker),
    )
    await db.commit()
    await db.refresh(broker)
    await _warm_cache(db)
    return broker


async def delete_broker(
    db: AsyncSession, broker_id, *, actor_id: Optional[UUID] = None
) -> None:
    """Hard-delete a broker.

    Referenced by ANY customer raises ``ValueError("broker in use by N
    customers")`` — the operator must re-point/remove those customers first.
    Deleting a non-existent broker is a no-op.
    """
    broker = await db.get(Broker, broker_id)
    if broker is None:
        return

    in_use = await _count_customers_for_code(db, broker.code)
    if in_use:
        raise ValueError(f"broker in use by {in_use} customers")

    before = _snapshot(broker)
    _record_audit(
        db,
        actor_id=actor_id,
        action="broker.delete",
        target_id=broker.id,
        before=before,
        after=None,
    )
    await db.delete(broker)
    await db.commit()
    await _warm_cache(db)


def _group_by_family(rows: list[Broker]) -> list[tuple[str, list[Broker]]]:
    """Group already-ordered broker ``rows`` by family for the dropdowns.

    Returns ``[("ephoenix", [...]), ("exir", [...])]`` — known families in the
    fixed ``ephoenix`` then ``exir`` order; any unexpected families (defensive)
    sorted alphabetically after. Empty families are omitted. ``rows`` are
    assumed to already be ordered by ``(sort_order, label)`` within a family
    (``list_brokers`` orders by ``(family, sort_order, label)``).
    """
    by_family: dict[str, list[Broker]] = {}
    for b in rows:
        by_family.setdefault(b.family, []).append(b)

    grouped: list[tuple[str, list[Broker]]] = []
    # Known families first, in display order.
    for fam in _FAMILY_ORDER:
        if by_family.get(fam):
            grouped.append((fam, by_family.pop(fam)))
    # Any unexpected families (defensive) sorted alphabetically after.
    for fam in sorted(by_family):
        grouped.append((fam, by_family[fam]))
    return grouped


async def list_enabled_grouped(
    db: AsyncSession,
) -> list[tuple[str, list[Broker]]]:
    """Enabled brokers grouped by family for the create-customer dropdown.

    Returns ``[("ephoenix", [...]), ("exir", [...])]`` — families in the fixed
    ``ephoenix`` then ``exir`` order; within each, ordered by
    ``(sort_order, label)``. Empty families are omitted.
    """
    rows = await list_brokers(db, include_disabled=False)
    return _group_by_family(rows)


async def list_all_grouped(db: AsyncSession) -> list[tuple[str, list[Broker]]]:
    """All brokers (incl. disabled) grouped by family — for the customer-list
    FILTER, where rows referencing a now-disabled broker must stay filterable.

    Same grouped shape as :func:`list_enabled_grouped`; forms keep using the
    enabled-only variant so they can't offer a disabled broker.
    """
    rows = await list_brokers(db, include_disabled=True)
    return _group_by_family(rows)


async def broker_codes(
    db: AsyncSession, *, enabled_only: bool = False
) -> set[str]:
    """Set of broker codes. ``enabled_only`` restricts to enabled rows."""
    stmt = select(Broker.code)
    if enabled_only:
        stmt = stmt.where(Broker.enabled.is_(True))
    result = await db.execute(stmt)
    return set(result.scalars().all())
