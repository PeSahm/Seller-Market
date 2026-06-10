"""Auto-balance an agent's customers across its stacks + auto-scale locust.

The trading bot's ``locustfile_new`` creates **one locust user-class per config
section** (one section = one customer×instrument) but locust only spawns a fixed
number of users across them, so with more sections than users the excess
customers are prepared but never fire. Two coupled fixes live here:

1. **Locust auto-scale** — the *render-time* part is in
   :func:`app.services.rendering.locust_config.compute_locust_targets`
   (``users = N×sections``, ``spawn_rate = sections``). This module re-pushes the
   affected stacks so the change reaches the host immediately.
2. **Load-balance** — :func:`reconcile_agent` redistributes a multi-stack agent's
   customers across its servers by **section count** (with hysteresis so a roughly
   balanced agent never thrashes), reusing the existing per-customer
   :func:`app.services.distribution.move_customer` engine.

Both run automatically on every stack push (hooked in
``stacks.provision_stack`` / ``stacks.redeploy_stack``) and on demand from the
``/admin/load-balance`` page. Everything is audited: per-item ``customer.move`` /
``stack.push_locust_config`` rows come free from the reused services, plus one
``autobalance.reconcile`` summary row written here.

Edge cases:

* **1 stack** → no balancing, only locust auto-scale.
* Customers are **indivisible** (a customer's N instructions move together), so a
  perfect even split may be off by one customer — that's acceptable.
* Moves to an **ineligible** (offline) server are skipped; a per-move failure is
  logged and the rest proceed (best-effort — never blocks a deploy).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.customers import Customer
from app.models.servers import Server
from app.models.stacks import AgentStack
from app.models.trade_instructions import TradeInstruction
from app.services.rendering.locust_config import compute_locust_targets

logger = logging.getLogger(__name__)

# Never auto-scale users below the legacy default — keeps tiny stacks sane.
_LOCUST_USERS_FLOOR = 10


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass
class StackLoad:
    """Per-stack load snapshot used for balancing + display."""

    stack_id: UUID
    server_id: UUID
    sections: int
    customers: int
    locust_users: int = 0
    locust_spawn_rate: int = 0


@dataclass
class PlannedMove:
    """One customer reassignment the balancer wants to make."""

    customer_id: UUID
    from_stack_id: UUID
    to_stack_id: UUID
    to_server_id: UUID
    sections: int


@dataclass
class ReconcileResult:
    """Outcome of a reconcile (or preview)."""

    agent_id: UUID
    num_stacks: int
    before: dict[UUID, StackLoad] = field(default_factory=dict)
    after: dict[UUID, StackLoad] = field(default_factory=dict)
    moves: list[PlannedMove] = field(default_factory=list)
    applied: bool = False
    balanced: bool = True
    message: str = ""


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_agent_stacks(
    db: AsyncSession, agent_id: UUID, *, eligible_only: bool = True
) -> list[AgentStack]:
    """Return an agent's stacks (one per server), newest-server-name first.

    ``eligible_only`` drops stacks whose server is offline / not-yet-seen so we
    never plan a move onto a dead box. Deprovisioning stacks are always excluded.
    """
    stmt = (
        select(AgentStack)
        .where(
            AgentStack.agent_id == agent_id,
            AgentStack.status != "deprovisioning",
        )
        .order_by(AgentStack.id)
    )
    stacks = list((await db.execute(stmt)).scalars().all())
    if not eligible_only:
        return stacks

    from app.services.distribution import _is_server_eligible  # noqa: WPS433

    out: list[AgentStack] = []
    for s in stacks:
        server = await db.get(Server, s.server_id)
        if server is not None and _is_server_eligible(server):
            out.append(s)
    return out


async def _load_customer_sections(
    db: AsyncSession, agent_id: UUID
) -> list[tuple[UUID, UUID, int]]:
    """``[(customer_id, stack_id, section_count)]`` for the agent's *assigned*
    customers. Section count = number of trade-instruction rows on the customer,
    EXCLUDING auto-sell-only rows — those are watch-only (no locust user fires at
    open), so they must not weigh the balancer nor inflate the locust targets.
    Keeping this in lock-step with the render-time count in
    ``render_locust_config`` is what stops the two from fighting on reconcile.
    """
    stmt = (
        select(
            Customer.id,
            Customer.stack_id,
            func.count(TradeInstruction.id),
        )
        .outerjoin(
            TradeInstruction,
            and_(
                TradeInstruction.customer_id == Customer.id,
                TradeInstruction.auto_sell_only.is_(False),
            ),
        )
        .where(
            Customer.agent_id == agent_id,
            Customer.stack_id.is_not(None),
        )
        .group_by(Customer.id, Customer.stack_id)
    )
    rows = await db.execute(stmt)
    return [(cid, sid, int(cnt or 0)) for cid, sid, cnt in rows.all()]


# ---------------------------------------------------------------------------
# Pure balancing core (no DB — unit-tested directly)
# ---------------------------------------------------------------------------


def _hysteresis_threshold(loads: dict[UUID, int]) -> int:
    """Only rebalance when ``max-min`` section gap exceeds this.

    ``max(2, ceil(0.15 × avg))`` — a couple of sections of slack, scaling with
    the agent's size, so a near-balanced agent never thrashes customers between
    servers (each move changes the customer's login IP — costly for some brokers).
    """
    n = len(loads) or 1
    avg = sum(loads.values()) / n
    return max(2, math.ceil(0.15 * avg))


def plan_moves(
    stack_ids: list[UUID],
    server_by_stack: dict[UUID, UUID],
    customer_loads: list[tuple[UUID, UUID, int]],
) -> list[PlannedMove]:
    """Compute the **minimal** set of whole-customer moves that brings the
    per-stack section load within the hysteresis threshold.

    Greedy: while the heaviest/lightest gap exceeds the threshold, move the
    customer from the heaviest stack whose section count best closes the gap
    (closest to half the gap, never larger than the gap so it strictly shrinks).
    Terminates because every move strictly reduces the gap; the outer cap is a
    safety net.
    """
    if len(stack_ids) < 2:
        return []

    load: dict[UUID, int] = {s: 0 for s in stack_ids}
    on: dict[UUID, list[tuple[UUID, int]]] = {s: [] for s in stack_ids}
    for cid, sid, sec in customer_loads:
        if sid in load:
            load[sid] += sec
            on[sid].append((cid, sec))

    if sum(load.values()) == 0:
        return []
    threshold = _hysteresis_threshold(load)

    moves: list[PlannedMove] = []
    for _ in range(len(customer_loads) + 1):  # safety cap
        heavy = max(stack_ids, key=lambda s: load[s])
        light = min(stack_ids, key=lambda s: load[s])
        gap = load[heavy] - load[light]
        if gap <= threshold:
            break
        # Candidates on the heavy stack that strictly shrink the gap (0<sec<gap);
        # pick the one closest to gap/2 (maximal reduction → fewest moves).
        candidates = [(cid, sec) for (cid, sec) in on[heavy] if 0 < sec < gap]
        if not candidates:
            break  # heavy's customers are all too big to move without overshoot
        cid, sec = min(candidates, key=lambda x: abs(gap - 2 * x[1]))

        load[heavy] -= sec
        load[light] += sec
        on[heavy].remove((cid, sec))
        on[light].append((cid, sec))
        moves.append(
            PlannedMove(
                customer_id=cid,
                from_stack_id=heavy,
                to_stack_id=light,
                to_server_id=server_by_stack[light],
                sections=sec,
            )
        )
    return moves


def _loads_from(
    stacks: list[AgentStack],
    customer_loads: list[tuple[UUID, UUID, int]],
    *,
    multiplier: int,
    floor_by_stack: Optional[dict[UUID, int]] = None,
) -> dict[UUID, StackLoad]:
    """Build per-stack :class:`StackLoad` (sections, customers, locust targets).

    ``floor_by_stack`` carries each stack's persisted ``LocustConfig.users`` so
    the previewed/audited ``locust_users`` matches what ``render_locust_config``
    actually renders (which uses that override as a floor). Falls back to the
    fleet floor for any stack without an override.
    """
    floor_by_stack = floor_by_stack or {}
    by_stack: dict[UUID, StackLoad] = {
        s.id: StackLoad(stack_id=s.id, server_id=s.server_id, sections=0, customers=0)
        for s in stacks
    }
    for _cid, sid, sec in customer_loads:
        sl = by_stack.get(sid)
        if sl is not None:
            sl.sections += sec
            sl.customers += 1
    for sl in by_stack.values():
        floor = floor_by_stack.get(sl.stack_id) or _LOCUST_USERS_FLOOR
        sl.locust_users, sl.locust_spawn_rate = compute_locust_targets(
            sl.sections, multiplier=multiplier, floor_users=floor
        )
    return by_stack


def _apply_moves(
    customer_loads: list[tuple[UUID, UUID, int]],
    moves: list[PlannedMove],
) -> list[tuple[UUID, UUID, int]]:
    """Return ``customer_loads`` with each move's customer reassigned (in-memory)."""
    dest = {mv.customer_id: mv.to_stack_id for mv in moves}
    return [(cid, dest.get(cid, sid), sec) for (cid, sid, sec) in customer_loads]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def reconcile_agent(
    db: AsyncSession,
    agent_id: UUID,
    actor_id: Optional[UUID],
    *,
    apply: bool = True,
    enable_balance: bool = True,
    multiplier: int = 3,
    skip_locust_push_for: Optional[UUID] = None,
) -> ReconcileResult:
    """Balance an agent's customers across its stacks + auto-scale locust.

    ``apply=False`` previews (no DB/SSH mutation) for the admin page. When
    applying, customer moves go through :func:`distribution.move_customer` (which
    audits + re-pushes both stacks' ``config.ini``) and each affected stack's
    ``locust_config.json`` is re-pushed via :func:`stacks.push_locust_config_for_stack`
    (which re-renders with the new section count). ``skip_locust_push_for`` skips
    the stack that the caller is about to deploy anyway (avoids a redundant push).
    A failure of any individual move/push is logged and swallowed — this must
    never block a deploy.
    """
    stacks = await list_agent_stacks(db, agent_id, eligible_only=True)
    if not stacks:
        return ReconcileResult(
            agent_id=agent_id, num_stacks=0, message="no eligible stacks"
        )

    server_by_stack = {s.id: s.server_id for s in stacks}
    stack_ids = [s.id for s in stacks]
    customer_loads = await _load_customer_sections(db, agent_id)

    # Each stack's persisted locust override acts as the auto-scale floor — load
    # it so preview/audit ``locust_users`` matches what render_locust_config emits.
    from app.services import locust_configs as services_locust  # noqa: WPS433

    floor_by_stack: dict[UUID, int] = {}
    for s in stacks:
        lc = await services_locust.get_locust_config(db, s.id)
        floor_by_stack[s.id] = lc.users if lc is not None else _LOCUST_USERS_FLOOR

    before = _loads_from(
        stacks, customer_loads, multiplier=multiplier, floor_by_stack=floor_by_stack
    )

    moves: list[PlannedMove] = []
    if len(stacks) > 1 and enable_balance:
        moves = plan_moves(stack_ids, server_by_stack, customer_loads)

    after_loads = _apply_moves(customer_loads, moves)
    after = _loads_from(
        stacks, after_loads, multiplier=multiplier, floor_by_stack=floor_by_stack
    )
    balanced = len(moves) == 0

    if not apply:
        return ReconcileResult(
            agent_id=agent_id,
            num_stacks=len(stacks),
            before=before,
            after=after,
            moves=moves,
            applied=False,
            balanced=balanced,
            message="preview",
        )

    # --- apply ---------------------------------------------------------------
    from app.services import distribution as dist  # noqa: WPS433
    from app.services import stacks as stacks_svc  # noqa: WPS433

    applied_moves: list[PlannedMove] = []
    for mv in moves:
        try:
            await dist.move_customer(
                db,
                mv.customer_id,
                new_server_id=mv.to_server_id,
                actor_id=actor_id,
            )
            applied_moves.append(mv)
        except Exception as exc:  # noqa: BLE001 — best-effort, never block deploy
            # Reset the shared session in case the failure left an open txn, so
            # later moves / the final commit don't inherit a poisoned session.
            await db.rollback()
            logger.warning(
                "autobalance: move customer %s -> server %s failed: %s",
                mv.customer_id,
                mv.to_server_id,
                exc,
            )

    # Re-push locust for stacks whose section count changed (plus, when run
    # standalone, all stacks so a section change without a move still scales).
    affected = {mv.from_stack_id for mv in applied_moves} | {
        mv.to_stack_id for mv in applied_moves
    }
    push_targets = affected if applied_moves else set(stack_ids)
    for sid in push_targets:
        if sid == skip_locust_push_for:
            continue
        try:
            await stacks_svc.push_locust_config_for_stack(db, sid, actor_id)
        except Exception as exc:  # noqa: BLE001 — best-effort
            await db.rollback()
            logger.warning("autobalance: push locust for stack %s failed: %s", sid, exc)

    # Recompute after-state from the moves that actually applied.
    after_loads = _apply_moves(customer_loads, applied_moves)
    after = _loads_from(
        stacks, after_loads, multiplier=multiplier, floor_by_stack=floor_by_stack
    )

    await _write_reconcile_audit(
        db, agent_id, actor_id, before, after, applied_moves
    )
    await db.commit()

    return ReconcileResult(
        agent_id=agent_id,
        num_stacks=len(stacks),
        before=before,
        after=after,
        moves=applied_moves,
        applied=True,
        balanced=len(applied_moves) == 0,
        message=(
            f"moved {len(applied_moves)} customer(s)"
            if applied_moves
            else "already balanced; locust scaled"
        ),
    )


def _stackload_json(loads: dict[UUID, StackLoad]) -> dict:
    return {
        str(sid): {
            "server_id": str(sl.server_id),
            "sections": sl.sections,
            "customers": sl.customers,
            "locust_users": sl.locust_users,
            "locust_spawn_rate": sl.locust_spawn_rate,
        }
        for sid, sl in loads.items()
    }


async def _write_reconcile_audit(
    db: AsyncSession,
    agent_id: UUID,
    actor_id: Optional[UUID],
    before: dict[UUID, StackLoad],
    after: dict[UUID, StackLoad],
    moves: list[PlannedMove],
) -> None:
    """One ``autobalance.reconcile`` summary row (per-item rows come from the
    reused move/push services). Skipped entirely when nothing changed, to keep
    the audit log quiet on the common already-balanced push."""
    if not moves and _stackload_json(before) == _stackload_json(after):
        return
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action="autobalance.reconcile",
            target_type="agent",
            target_id=str(agent_id),
            before_json={"stacks": _stackload_json(before)},
            after_json={
                "stacks": _stackload_json(after),
                "moves": [
                    {
                        "customer_id": str(m.customer_id),
                        "from_stack_id": str(m.from_stack_id),
                        "to_stack_id": str(m.to_stack_id),
                        "sections": m.sections,
                    }
                    for m in moves
                ],
            },
        )
    )


__all__ = [
    "StackLoad",
    "PlannedMove",
    "ReconcileResult",
    "list_agent_stacks",
    "plan_moves",
    "reconcile_agent",
    "compute_locust_targets",
]
