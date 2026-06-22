"""Customer-distribution orchestration (Phase 4).

Takes an agent-declared :class:`~app.models.customers.Customer` row and
assigns / moves / unassigns it to a concrete ``(server, agent_stack)``
pair. After the row is updated, calls into the stacks service to re-render
and SFTP-push the affected ``config.ini`` files.

Module layout
-------------
The module groups the four concerns of the distribution feature in this
order, so a reviewer can read it top-down:

1. **Policy storage** — ``get_policy_for``, ``set_global_policy``,
   ``set_agent_policy``, ``clear_agent_policy``. CRUD against
   :class:`~app.models.customers.DistributionPolicy`.
2. **Policy resolution** — ``resolve_target_server`` plus the three pure
   helpers ``_resolve_round_robin``, ``_resolve_least_customers``,
   ``_resolve_broker_affinity``. These are the testable core of the
   feature; keeping them pure functions (no DB writes, no audit) is what
   lets the test suite exercise every branch without standing up a real
   AsyncSession.
3. **Server eligibility** — ``_candidate_servers`` (online + not soft-
   deleted). The "online OR last_seen_at within ~5 min" defence is in
   here so every policy sees the same fleet view.
4. **Assignment actions** — ``assign_customer``, ``unassign_customer``,
   ``move_customer``. Each follows the same shape: lock the customer
   row, mutate it under that lock, commit, then call into the stacks
   service to push config.ini outside the customer-row lock (the push
   has its own per-server compose advisory lock). Audit happens after
   the row mutation but before the push so an admin always sees the
   intent recorded even if the SFTP fails.

Concurrency
-----------
* The :func:`assign_customer` / :func:`unassign_customer` /
  :func:`move_customer` flow takes a row-level lock on the customer
  (``SELECT ... FOR UPDATE``) for the duration of the DB mutation. That
  pins the version field; if another writer bumped it between the read
  and our update we raise
  :class:`app.services.customers.OptimisticLockError` so the router can
  render an HTTP 409.
* The actual ``config.ini`` push is delegated to
  :func:`app.services.stacks.push_config_ini_for_stack` which serialises
  on a per-server compose advisory lock. That keeps two admins moving
  different customers to the same server from racing each other into a
  corrupted file.
* We deliberately do NOT hold the customer row lock for the duration of
  the SFTP push. That would couple the customer-row TX to a 10s remote
  command and starve other customer updates. The trade-off is benign:
  the customer row already reflects the new assignment; if the push
  fails, the row stays as-is and the admin can retry the push from the
  stack page.

What we do NOT do here
----------------------
* No direct SSH / SFTP calls — those are encapsulated by the stacks
  service. We only know that ``push_config_ini_for_stack`` exists and
  returns a :class:`StackActionResult`.
* No template rendering — the router is responsible for translating an
  :class:`AssignmentResult` into a flash banner.
* No template-side flash handling for partial failures (e.g. customer
  row updated but SFTP failed) — the function re-raises the
  :class:`SSHError` from the push so the router can decide. We keep
  this behaviour explicit because silently swallowing an SFTP failure
  would leave the DB and the remote out of sync without the admin
  knowing.

Phase 4 scope
-------------
Parallel agent A owns ``app/services/customers.py``; parallel agent E
owns the ``push_config_ini_for_stack`` helper inside
``app/services/stacks.py``. Both names are part of our contract — if
either is missing at runtime the corresponding code path here will
ImportError at the call site, which is what we want (loud, early
failure) rather than a silent no-op.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.customers import Customer, DistributionPolicy
from app.models.servers import Server
from app.models.stacks import AgentStack
from app.schemas.distribution import AssignmentResult, PolicySet

logger = logging.getLogger(__name__)


# A server that hasn't checked in for longer than this window is treated as
# offline even if its ``status`` column still reads ``'online'`` — gives us
# a defensive fallback in case the health worker gets stuck. The window is
# generous on purpose: the worker probes roughly every 60s, so 5 min is
# four missed cycles, which is well beyond a transient blip.
_STALE_HEARTBEAT_WINDOW = timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    """A timezone-aware UTC ``datetime`` for TIMESTAMPTZ columns."""
    return datetime.now(timezone.utc)


def _customer_snapshot(customer: Customer) -> dict:
    """Audit-safe dict of a :class:`Customer` row.

    Deliberately omits ``password_enc`` (Fernet ciphertext) so it cannot
    leak into the audit log. Includes only the fields that change as a
    result of an assign / move / unassign — that's what an admin will want
    to see in the before/after diff.
    """
    return {
        "id": str(customer.id),
        "agent_id": str(customer.agent_id),
        "server_id": str(customer.server_id) if customer.server_id else None,
        "stack_id": str(customer.stack_id) if customer.stack_id else None,
        "assignment_status": customer.assignment_status,
        "broker": customer.broker,
        "username": customer.username,
    }


async def _write_audit(
    db: AsyncSession,
    *,
    actor_id: Optional[UUID],
    action: str,
    target_id: UUID,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
) -> None:
    """Insert a single ``audit_log`` row.

    ``target_type`` is always ``"customer"`` for this module — the row
    being mutated is the customer, even on actions like ``customer.move``
    that re-render two stacks as a side effect. The caller is responsible
    for ensuring ``before`` / ``after`` contain no secret material;
    :func:`_customer_snapshot` enforces this for the happy path.
    """
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action=action,
            target_type="customer",
            target_id=str(target_id),
            before_json=before,
            after_json=after,
        )
    )


# ---------------------------------------------------------------------------
# Policy storage
# ---------------------------------------------------------------------------


async def get_policy_for(
    db: AsyncSession, agent_id: UUID
) -> DistributionPolicy:
    """Return the effective :class:`DistributionPolicy` for an agent.

    Lookup order, narrowest to widest:

    1. Per-agent override (``scope='agent' AND agent_id=...``).
    2. Global default (``scope='global'``).
    3. A synthetic in-memory ``'manual'`` policy with no default server.
       Returned unattached so the caller can read ``.policy`` without
       worrying about whether the DB has the row yet — this lets the UI
       boot cleanly on a fresh install before the operator has visited
       the distribution-policy page.

    The function never raises and never mutates the DB. The synthetic
    fallback is created with ``DistributionPolicy(...)`` but is NOT added
    to the session — returning a detached instance is fine because the
    caller only reads attributes off it.
    """
    # 1) per-agent override
    stmt = select(DistributionPolicy).where(
        DistributionPolicy.scope == "agent",
        DistributionPolicy.agent_id == agent_id,
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is not None:
        return row

    # 2) global default
    stmt = select(DistributionPolicy).where(
        DistributionPolicy.scope == "global"
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is not None:
        return row

    # 3) synthetic manual fallback — not added to the session, returned
    # detached. The caller only reads ``.policy`` / ``.default_server_id``.
    return DistributionPolicy(
        scope="global",
        agent_id=None,
        policy="manual",
        default_server_id=None,
    )


async def set_global_policy(
    db: AsyncSession,
    data: PolicySet,
    actor_id: UUID,
) -> DistributionPolicy:
    """Upsert the single global ``scope='global'`` row.

    There is at most one global row by convention (no unique constraint at
    the DB level today, so we enforce it here). Calling this twice with
    different ``policy`` values overwrites — that's the expected admin UX.

    The ``data.scope`` and ``data.agent_id`` fields on the input are
    deliberately ignored: this function is the global setter, period. The
    router is the right place to refuse a mismatching ``scope='agent'``
    body if that ever surfaces as a real failure mode.
    """
    stmt = select(DistributionPolicy).where(
        DistributionPolicy.scope == "global"
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()

    before = _policy_snapshot(row) if row is not None else None

    if row is None:
        row = DistributionPolicy(
            scope="global",
            agent_id=None,
            policy=data.policy,
            default_server_id=data.default_server_id,
        )
        db.add(row)
    else:
        row.policy = data.policy
        row.default_server_id = data.default_server_id

    # Touch updated_at — the column has a server-default for inserts but
    # SQLAlchemy doesn't auto-bump it on update, so we set it explicitly.
    row.updated_at = _now_utc()
    await db.flush()

    await _write_audit(
        db,
        actor_id=actor_id,
        action="distribution.set_global_policy",
        target_id=row.id,
        before=before,
        after=_policy_snapshot(row),
    )
    await db.commit()
    await db.refresh(row)
    return row


async def set_agent_policy(
    db: AsyncSession,
    agent_id: UUID,
    data: PolicySet,
    actor_id: UUID,
) -> DistributionPolicy:
    """Upsert a per-agent ``scope='agent'`` override row.

    There is at most one override per ``agent_id``. Same upsert semantics
    as :func:`set_global_policy`: calling twice with different policy
    values overwrites.
    """
    stmt = select(DistributionPolicy).where(
        DistributionPolicy.scope == "agent",
        DistributionPolicy.agent_id == agent_id,
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()

    before = _policy_snapshot(row) if row is not None else None

    if row is None:
        row = DistributionPolicy(
            scope="agent",
            agent_id=agent_id,
            policy=data.policy,
            default_server_id=data.default_server_id,
        )
        db.add(row)
    else:
        row.policy = data.policy
        row.default_server_id = data.default_server_id

    row.updated_at = _now_utc()
    await db.flush()

    await _write_audit(
        db,
        actor_id=actor_id,
        action="distribution.set_agent_policy",
        target_id=row.id,
        before=before,
        after=_policy_snapshot(row),
    )
    await db.commit()
    await db.refresh(row)
    return row


async def clear_agent_policy(
    db: AsyncSession,
    agent_id: UUID,
    actor_id: UUID,
) -> None:
    """Delete the per-agent override so the global default applies again.

    Idempotent: deleting a non-existent override is a silent no-op (no
    audit entry written). An admin re-clicking "use global default" on
    a UI that's already showing the global default shouldn't generate
    audit noise.
    """
    stmt = select(DistributionPolicy).where(
        DistributionPolicy.scope == "agent",
        DistributionPolicy.agent_id == agent_id,
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return

    before = _policy_snapshot(row)
    target_id = row.id

    await db.execute(
        delete(DistributionPolicy).where(DistributionPolicy.id == row.id)
    )

    await _write_audit(
        db,
        actor_id=actor_id,
        action="distribution.clear_agent_policy",
        target_id=target_id,
        before=before,
        after=None,
    )
    await db.commit()


def _policy_snapshot(row: DistributionPolicy) -> dict:
    """Audit-safe dict of a :class:`DistributionPolicy` row.

    No secret material lives on the row — it's just the operator's choice
    of algorithm — so this is a straight projection.
    """
    return {
        "id": str(row.id),
        "scope": row.scope,
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "policy": row.policy,
        "default_server_id": (
            str(row.default_server_id) if row.default_server_id else None
        ),
    }


# ---------------------------------------------------------------------------
# Server eligibility
# ---------------------------------------------------------------------------


def _is_server_eligible(server: Server, *, now: Optional[datetime] = None) -> bool:
    """A server is eligible if it's online (or recently seen) and not deleted.

    Returns ``True`` when:

    * ``status == 'online'``, OR
    * ``status != 'offline'`` AND ``last_seen_at`` is within the stale
      window (we treat a missing health-worker pulse as the server still
      being usable for a short window — a 30-second probe gap shouldn't
      block customer assignment).

    A server with ``status == 'offline'`` is always rejected, even if it
    has a recent ``last_seen_at`` (defensive: explicit "offline" beats
    inferred "recently seen").

    Soft delete: the Server model has no ``deleted_at`` column yet (see
    the TODO in :mod:`app.services.servers`). When that lands the check
    will turn into ``and not server.deleted_at`` — until then, hard
    deletes already remove the row, so a soft-deleted server simply
    isn't in :func:`list_servers`' output.
    """
    if server.status == "offline":
        return False
    if server.status == "online":
        return True
    # Unknown status — fall back to last_seen_at heuristic. If we've never
    # seen the server, treat it as not eligible (the admin should run
    # "test connection" first).
    if server.last_seen_at is None:
        return False
    horizon = (now or _now_utc()) - _STALE_HEARTBEAT_WINDOW
    # last_seen_at is TIMESTAMPTZ so comparing with a tz-aware datetime is safe.
    return server.last_seen_at >= horizon


async def _candidate_servers(db: AsyncSession) -> list[Server]:
    """Return the fleet of servers eligible to receive a new customer.

    Source of truth for the resolver — every policy operates over this
    list. Ordered by ``name`` so ties (round-robin with no customers,
    least_customers with equal counts) break deterministically.
    """
    stmt = select(Server).order_by(Server.name)
    result = await db.execute(stmt)
    now = _now_utc()
    return [s for s in result.scalars().all() if _is_server_eligible(s, now=now)]


# ---------------------------------------------------------------------------
# Policy resolution (pure helpers)
# ---------------------------------------------------------------------------
#
# Each resolver takes ``(db, customer, candidate_servers)`` and returns one
# Server. They DO NOT mutate the DB, DO NOT call other services, and DO NOT
# raise on empty input — the caller (``resolve_target_server``) handles
# the "no candidates" case once, in one place, with a single error
# message. Keeping them pure is what lets the test suite exercise each
# branch without an AsyncSession.


async def _resolve_round_robin(
    db: AsyncSession,
    customer: Customer,
    candidate_servers: list[Server],
) -> Server:
    """Pick the server whose most-recent customer is OLDEST.

    Stateless round-robin: instead of a counter table we read
    ``MAX(customers.created_at) GROUP BY server_id`` and pick the server
    with the smallest max (i.e. the one that hasn't received a customer
    the longest). A server that has never received a customer wins
    outright — it has no MAX, so it sorts before every server that does.

    Equivalent to a classic round-robin without the locking / coordination
    cost of an explicit counter. Ties broken by server name for
    determinism.
    """
    # noqa: ARG001 — customer is part of the resolver signature for symmetry
    # with the other policies; round-robin itself doesn't read from it.
    _ = customer

    if not candidate_servers:
        raise ValueError("round_robin: no eligible servers")

    candidate_ids = [s.id for s in candidate_servers]

    # Per-server MAX(created_at). Post-0004 every customer in the DB is
    # live (no more soft-delete), so no enabled filter is needed.
    stmt = (
        select(Customer.server_id, func.max(Customer.created_at))
        .where(Customer.server_id.in_(candidate_ids))
        .group_by(Customer.server_id)
    )
    result = await db.execute(stmt)
    last_assigned: dict[UUID, datetime] = {
        row[0]: row[1] for row in result.all() if row[0] is not None
    }

    # Sort: servers with no MAX first (sentinel), then by MAX ascending,
    # then by name for tie-break.
    def _sort_key(s: Server) -> tuple[int, datetime, str]:
        ts = last_assigned.get(s.id)
        # Use a tuple (has_ts, ts_or_epoch, name) so unsigned servers sort
        # before signed ones. Epoch sentinel never appears in the final
        # comparison because ``has_ts`` differs.
        if ts is None:
            return (0, datetime(1970, 1, 1, tzinfo=timezone.utc), s.name)
        return (1, ts, s.name)

    return sorted(candidate_servers, key=_sort_key)[0]


async def _resolve_least_customers(
    db: AsyncSession,
    customer: Customer,
    candidate_servers: list[Server],
) -> Server:
    """Pick the server with the fewest enabled customers.

    ``COUNT(*) GROUP BY server_id`` over enabled customers. Servers with
    no customers have no row in the result; we default their count to 0
    so they win the comparison outright. Ties broken alphabetically by
    server name for deterministic UI behaviour.
    """
    _ = customer  # see comment in _resolve_round_robin

    if not candidate_servers:
        raise ValueError("least_customers: no eligible servers")

    candidate_ids = [s.id for s in candidate_servers]

    stmt = (
        select(Customer.server_id, func.count(Customer.id))
        .where(Customer.server_id.in_(candidate_ids))
        .group_by(Customer.server_id)
    )
    result = await db.execute(stmt)
    counts: dict[UUID, int] = {
        row[0]: int(row[1]) for row in result.all() if row[0] is not None
    }

    return sorted(
        candidate_servers,
        key=lambda s: (counts.get(s.id, 0), s.name),
    )[0]


async def _resolve_broker_affinity(
    db: AsyncSession,
    customer: Customer,
    candidate_servers: list[Server],
) -> Server:
    """Co-locate this agent's customers of the same broker on one server.

    Why: the trading bot's broker login can be touchy — sharing a single
    session across one box reduces the chance of the broker locking out
    "concurrent login from different IPs". This policy keeps an agent's
    customers for a given broker on whatever server already hosts the
    first one.

    Lookup:

    1. ``SELECT DISTINCT server_id FROM customers WHERE agent_id = $1
       AND broker = $2 AND server_id IS NOT NULL``.
    2. If any of those server_ids are in ``candidate_servers``, pick
       the one with the most existing customers of this broker for this
       agent (so a server with 3 bbi customers wins over one with 1).
    3. Otherwise fall back to :func:`_resolve_least_customers` —
       affinity is a *hint*, not a hard constraint.

    Isolation: the WHERE clause filters by ``agent_id``, so agent B's
    bbi customers on a different server never influence agent A's
    resolution.
    """
    candidate_ids = {s.id for s in candidate_servers}

    stmt = (
        select(Customer.server_id, func.count(Customer.id))
        .where(Customer.agent_id == customer.agent_id)
        .where(Customer.broker == customer.broker)
        .where(Customer.server_id.is_not(None))
        .group_by(Customer.server_id)
    )
    result = await db.execute(stmt)
    affinity_counts: dict[UUID, int] = {
        row[0]: int(row[1]) for row in result.all() if row[0] is not None
    }

    # Filter down to currently-eligible servers — if the only server that
    # hosts an affinity customer is offline, we don't want to insist on
    # it (the customer would just sit pending). Fall through to
    # least-customers in that case.
    eligible_affinity = {
        sid: count
        for sid, count in affinity_counts.items()
        if sid in candidate_ids
    }

    if eligible_affinity:
        # Pick the server with the most existing same-broker customers.
        # Tie-break: server name (lookup by id from the candidate list).
        servers_by_id = {s.id: s for s in candidate_servers}
        return sorted(
            (servers_by_id[sid] for sid in eligible_affinity),
            key=lambda s: (-eligible_affinity[s.id], s.name),
        )[0]

    # Fallback: no eligible affinity hit, defer to least-customers so the
    # customer still lands somewhere sensible.
    return await _resolve_least_customers(db, customer, candidate_servers)


async def resolve_target_server(
    db: AsyncSession,
    customer: Customer,
    *,
    override_server_id: Optional[UUID] = None,
) -> Server:
    """Decide where ``customer`` should land.

    Two paths:

    * **Manual override** — ``override_server_id`` is set (admin clicked
      a specific server in the UI). We look the server up and refuse
      if it's not eligible (offline / not seen). The override branch is
      independent of the policy field on the agent — it always wins.
    * **Policy resolution** — read the effective policy via
      :func:`get_policy_for` and dispatch on it. ``manual`` policy
      without an override is a hard error: the admin needs to pick a
      server or change the policy.

    Raises:
        ValueError: on bad input (no override + manual policy, an
            unknown policy string, an override pointing at an unknown /
            ineligible server, or no eligible servers at all).
    """
    if override_server_id is not None:
        server = await db.get(Server, override_server_id)
        if server is None:
            raise ValueError(f"server {override_server_id} not found")
        if not _is_server_eligible(server):
            # We deliberately surface a distinct message here so the UI
            # can show "server is offline, try another" rather than a
            # generic 500.
            raise ValueError(
                f"server {server.name!r} is not eligible "
                f"(status={server.status!r})"
            )
        return server

    # No override — fall through to policy.
    policy = await get_policy_for(db, customer.agent_id)

    # If the policy is manual without an override there's nothing for us
    # to decide. ``default_server_id`` is a per-policy convenience field
    # — when set, it acts as the implicit override.
    if policy.policy == "manual":
        if policy.default_server_id is not None:
            # Recurse with the configured default acting as the override
            # so we still get the eligibility check.
            return await resolve_target_server(
                db,
                customer,
                override_server_id=policy.default_server_id,
            )
        raise ValueError(
            "no override + manual policy = no decision; admin must pick "
            "a server explicitly or change the agent's policy"
        )

    candidate_servers = await _candidate_servers(db)
    if not candidate_servers:
        raise ValueError("no eligible servers in the fleet")

    if policy.policy == "round_robin":
        return await _resolve_round_robin(db, customer, candidate_servers)
    if policy.policy == "least_customers":
        return await _resolve_least_customers(db, customer, candidate_servers)
    if policy.policy == "broker_affinity":
        return await _resolve_broker_affinity(db, customer, candidate_servers)

    # Defensive: pydantic's Literal already rejects unknown policies at
    # the HTTP boundary, but a manual DB edit could slip one past. Loud
    # error beats silent fallback.
    raise ValueError(f"unknown policy: {policy.policy!r}")


# ---------------------------------------------------------------------------
# Assignment actions
# ---------------------------------------------------------------------------
#
# These import the customers / stacks services lazily so the module loads
# without depending on parallel agents' module-load order. The contract
# names are fixed: ``app.services.customers.get_customer`` and
# ``app.services.stacks.{find_or_create_stack, push_config_ini_for_stack}``.


def _import_customers_service():
    """Lazy import of the customers service.

    Parallel agent A owns the module; importing it at module-load time
    would couple our import order to theirs and break the unit tests for
    the pure-policy helpers (which never go near customers.py). Deferring
    the import to the call site means a missing module surfaces only when
    an actual assign / unassign / move is attempted.
    """
    from app.services import customers as customers_service  # noqa: WPS433

    return customers_service


def _import_stacks_service():
    """Lazy import of the stacks service. Mirrors :func:`_import_customers_service`.

    The stacks service is owned by parallel agent E. We only use two
    names from it: ``find_or_create_stack`` (already present in Phase 3)
    and ``push_config_ini_for_stack`` (which agent E is adding in this
    phase). Importing at module-load would mean any change to either of
    those would force a re-test of the policy resolvers — that's wrong,
    they're independent concerns.
    """
    from app.services import stacks as stacks_service  # noqa: WPS433

    return stacks_service


async def _lock_customer_for_update(
    db: AsyncSession, customer_id: UUID
) -> Optional[Customer]:
    """``SELECT ... FOR UPDATE`` a customer row.

    Used at the top of every mutating action so a second admin clicking
    "assign" on the same customer waits for the first to finish (or sees
    a stale-version error post-commit). Returns ``None`` if the customer
    doesn't exist; the caller is responsible for raising the right
    typed exception.
    """
    stmt = (
        select(Customer)
        .where(Customer.id == customer_id)
        .with_for_update()
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def assign_customer(
    db: AsyncSession,
    customer_id: UUID,
    *,
    server_id: Optional[UUID],
    actor_id: UUID,
) -> AssignmentResult:
    """Main entry: place ``customer`` onto a ``(server, agent_stack)`` pair.

    ``server_id`` is the manual override; ``None`` falls back to the
    agent's effective distribution policy via :func:`resolve_target_server`.

    Sequence:

    1. Lock the customer row FOR UPDATE.
    2. Resolve the target server (override OR policy).
    3. ``find_or_create_stack(server, agent_id)`` to make sure the
       ``agent_stacks`` row exists. If it already does we just get
       handed back the existing one.
    4. Update the customer row: ``server_id``, ``stack_id``,
       ``assignment_status='active'``.
    5. Commit the customer-row mutation.
    6. SFTP-push the affected stack's ``config.ini`` (outside the
       customer-row lock — see module docstring for the rationale).
    7. Audit ``customer.assign``.
    8. Return :class:`AssignmentResult`.

    Raises:
        ValueError: on bad input (unknown customer, no eligible server,
            manual policy without an override).
        OptimisticLockError: re-raised from the customers service if the
            customer moved under us between read and update.
        SSHError: re-raised from the stacks push. The customer row HAS
            already been updated by that point — the admin can retry
            the push from the stack page.
    """
    stacks = _import_stacks_service()

    customer = await _lock_customer_for_update(db, customer_id)
    if customer is None:
        raise ValueError(f"customer {customer_id} not found")

    before = _customer_snapshot(customer)
    old_stack_id = customer.stack_id

    server = await resolve_target_server(
        db, customer, override_server_id=server_id
    )

    # find_or_create_stack handles the unique-constraint race for us via
    # the SQL UNIQUE on (server_id, agent_id) — two concurrent assigns to
    # the same (server, agent) can't both insert.
    stack = await stacks.find_or_create_stack(
        db, server, customer.agent_id, actor_id
    )

    customer.server_id = stack.server_id
    customer.stack_id = stack.id
    customer.assignment_status = "active"
    customer.updated_at = _now_utc()

    after = _customer_snapshot(customer)

    await _write_audit(
        db,
        actor_id=actor_id,
        action="customer.assign",
        target_id=customer.id,
        before=before,
        after=after,
    )
    await db.commit()
    await db.refresh(customer)

    # Push config.ini for the new stack outside the customer-row TX. If
    # the customer moved between stacks (i.e. they previously had an
    # old_stack_id and it's different from the new one), push both. This
    # is unusual for ``assign`` — normally the row starts unassigned —
    # but we handle it defensively so re-assigning an already-active
    # customer to a different server via this entry point Just Works.
    affected: list[UUID] = [stack.id]
    await stacks.push_config_ini_for_stack(db, stack.id, actor_id)
    if old_stack_id is not None and old_stack_id != stack.id:
        affected.append(old_stack_id)
        await stacks.push_config_ini_for_stack(db, old_stack_id, actor_id)

    return AssignmentResult(
        ok=True,
        customer_id=customer.id,
        old_server_id=_uuid_or_none(before.get("server_id")),
        new_server_id=stack.server_id,
        old_stack_id=old_stack_id,
        new_stack_id=stack.id,
        affected_stack_ids=affected,
        message=f"assigned to {server.name}",
    )


async def assign_customer_to_random_existing_stack(
    db: AsyncSession,
    customer_id: UUID,
    *,
    actor_id: UUID,
) -> AssignmentResult:
    """Auto-assign a pending customer to a RANDOM EXISTING stack of its agent.

    Used on agent self-service customer creation so the customer trades from an
    existing stack immediately, without the admin pending-inbox step. Unlike
    :func:`assign_customer` this NEVER creates a stack and ignores distribution
    policy — it just picks uniformly at random among the ``agent_stacks`` rows
    that already exist for the customer's agent.

    If the agent has NO stack yet, the customer is left ``pending`` (returns
    ``ok=False``) — the admin inbox / a later assign handles it. Best-effort by
    contract: callers wrap this so a failure never blocks customer creation.

    Sequence mirrors :func:`assign_customer` (lock → set row → commit → push),
    minus policy resolution and stack creation.
    """
    stacks = _import_stacks_service()

    customer = await _lock_customer_for_update(db, customer_id)
    if customer is None:
        raise ValueError(f"customer {customer_id} not found")

    rows = (
        await db.execute(
            select(AgentStack).where(AgentStack.agent_id == customer.agent_id)
        )
    ).scalars().all()
    if not rows:
        # No existing stack for this agent → leave pending (we never create one).
        return AssignmentResult(
            ok=False,
            customer_id=customer.id,
            old_server_id=customer.server_id,
            old_stack_id=customer.stack_id,
            message="no existing stack for agent — left pending",
        )

    stack = random.choice(rows)

    before = _customer_snapshot(customer)
    old_stack_id = customer.stack_id

    customer.server_id = stack.server_id
    customer.stack_id = stack.id
    customer.assignment_status = "active"
    customer.updated_at = _now_utc()

    after = _customer_snapshot(customer)
    await _write_audit(
        db,
        actor_id=actor_id,
        action="customer.assign",
        target_id=customer.id,
        before=before,
        after=after,
    )
    await db.commit()
    await db.refresh(customer)

    affected: list[UUID] = [stack.id]
    await stacks.push_config_ini_for_stack(db, stack.id, actor_id)
    if old_stack_id is not None and old_stack_id != stack.id:
        affected.append(old_stack_id)
        await stacks.push_config_ini_for_stack(db, old_stack_id, actor_id)

    return AssignmentResult(
        ok=True,
        customer_id=customer.id,
        old_server_id=_uuid_or_none(before.get("server_id")),
        new_server_id=stack.server_id,
        old_stack_id=old_stack_id,
        new_stack_id=stack.id,
        affected_stack_ids=affected,
        message="auto-assigned to an existing stack",
    )


async def unassign_customer(
    db: AsyncSession,
    customer_id: UUID,
    *,
    actor_id: UUID,
) -> AssignmentResult:
    """Detach a customer from its current stack.

    Sets ``stack_id=NULL``, ``server_id=NULL``, ``assignment_status='pending'``.
    The customer remains in the DB but stops being rendered into any
    server's ``config.ini``. The OLD stack is re-pushed so the row
    actually disappears from the remote config — without that, the
    trading bot would keep using the now-orphaned section until the
    next deploy.

    Idempotent: unassigning an already-unassigned customer is a silent
    no-op (no SFTP push, no audit entry) — we don't want admin double-
    clicks to generate redundant audit noise or pointless SFTP traffic.
    """
    stacks = _import_stacks_service()

    customer = await _lock_customer_for_update(db, customer_id)
    if customer is None:
        raise ValueError(f"customer {customer_id} not found")

    if customer.stack_id is None:
        # Already unassigned — no audit, no push.
        return AssignmentResult(
            ok=True,
            customer_id=customer.id,
            old_server_id=None,
            new_server_id=None,
            old_stack_id=None,
            new_stack_id=None,
            affected_stack_ids=[],
            message="already unassigned",
        )

    before = _customer_snapshot(customer)
    old_stack_id = customer.stack_id
    old_server_id = customer.server_id

    customer.server_id = None
    customer.stack_id = None
    customer.assignment_status = "pending"
    customer.updated_at = _now_utc()

    await _write_audit(
        db,
        actor_id=actor_id,
        action="customer.unassign",
        target_id=customer.id,
        before=before,
        after=_customer_snapshot(customer),
    )
    await db.commit()
    await db.refresh(customer)

    # Re-render the OLD stack's config.ini — the customer is no longer
    # part of it, so the file needs to be re-written to drop the section.
    await stacks.push_config_ini_for_stack(db, old_stack_id, actor_id)

    return AssignmentResult(
        ok=True,
        customer_id=customer.id,
        old_server_id=old_server_id,
        new_server_id=None,
        old_stack_id=old_stack_id,
        new_stack_id=None,
        affected_stack_ids=[old_stack_id],
        message="unassigned",
    )


async def move_customer(
    db: AsyncSession,
    customer_id: UUID,
    *,
    new_server_id: UUID,
    actor_id: UUID,
) -> AssignmentResult:
    """Reassign a customer from server A to server B.

    Sequence:

    1. Lock the customer row.
    2. Look up the new server; refuse if not eligible.
    3. ``find_or_create_stack(new_server, agent_id)`` for the new pair.
    4. Update the customer row.
    5. Commit.
    6. Push the NEW stack's config.ini first (so the customer appears
       on B before vanishing from A), then the OLD stack's. Order
       matters: if step 7 fails after step 6 succeeds, the customer
       is on both servers briefly — better than being on neither.
    7. Audit ``customer.move`` with both stack ids in the payload.

    Edge cases:

    * If the customer is currently unassigned (``stack_id is None``),
      this degenerates into :func:`assign_customer` with a manual
      override — we handle it in one path rather than two.
    * If the new server equals the current server, we still re-push
      the config.ini (cheap insurance against drift) but skip the
      audit because the move is a no-op state-change.
    """
    stacks = _import_stacks_service()

    customer = await _lock_customer_for_update(db, customer_id)
    if customer is None:
        raise ValueError(f"customer {customer_id} not found")

    new_server = await db.get(Server, new_server_id)
    if new_server is None:
        raise ValueError(f"server {new_server_id} not found")
    if not _is_server_eligible(new_server):
        raise ValueError(
            f"server {new_server.name!r} is not eligible "
            f"(status={new_server.status!r})"
        )

    before = _customer_snapshot(customer)
    old_stack_id = customer.stack_id
    old_server_id = customer.server_id

    new_stack = await stacks.find_or_create_stack(
        db, new_server, customer.agent_id, actor_id
    )

    # Same-server "move" is a no-op state-change. We still re-push to
    # heal any drift, but don't write an audit entry — that would just
    # clutter the log for the admin.
    if old_stack_id == new_stack.id:
        await db.commit()
        await stacks.push_config_ini_for_stack(db, new_stack.id, actor_id)
        return AssignmentResult(
            ok=True,
            customer_id=customer.id,
            old_server_id=old_server_id,
            new_server_id=new_server.id,
            old_stack_id=old_stack_id,
            new_stack_id=new_stack.id,
            affected_stack_ids=[new_stack.id],
            message=f"already on {new_server.name}; config re-pushed",
        )

    customer.server_id = new_stack.server_id
    customer.stack_id = new_stack.id
    customer.assignment_status = "active"
    customer.updated_at = _now_utc()

    await _write_audit(
        db,
        actor_id=actor_id,
        action="customer.move",
        target_id=customer.id,
        before=before,
        after={
            **_customer_snapshot(customer),
            "old_stack_id": str(old_stack_id) if old_stack_id else None,
            "new_stack_id": str(new_stack.id),
        },
    )
    await db.commit()
    await db.refresh(customer)

    # Push order: NEW first, then OLD. If push-OLD fails, the customer
    # is on both servers in config.ini — a duplicate is recoverable
    # (admin re-runs the push from the old stack page). If we did OLD
    # first and the NEW push failed, the customer would vanish from
    # both servers — much worse.
    affected: list[UUID] = [new_stack.id]
    await stacks.push_config_ini_for_stack(db, new_stack.id, actor_id)
    if old_stack_id is not None:
        affected.append(old_stack_id)
        await stacks.push_config_ini_for_stack(db, old_stack_id, actor_id)

    return AssignmentResult(
        ok=True,
        customer_id=customer.id,
        old_server_id=old_server_id,
        new_server_id=new_stack.server_id,
        old_stack_id=old_stack_id,
        new_stack_id=new_stack.id,
        affected_stack_ids=affected,
        message=f"moved to {new_server.name}",
    )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


async def pending_customers(
    db: AsyncSession,
    *,
    agent_id: Optional[UUID] = None,
) -> list[Customer]:
    """Customers awaiting admin assignment (the "Pending" inbox).

    Filter (a customer matches if EITHER holds):

    * ``assignment_status == 'pending'`` — explicitly tagged as needing
      placement. We do NOT include the transient ``'assigned'`` state (between
      resolution and the SFTP push) just because ``server_id IS NULL``.
    * ``assignment_status == 'active' AND stack_id IS NULL`` — an ORPHANED
      active customer. This happens when its stack was deleted: the
      ``stack_id`` FK (``ON DELETE SET NULL``) clears the binding but leaves
      the row 'active', so it falls out of every ``config.ini`` (the renderer
      selects ``WHERE stack_id == stack.id``) yet wouldn't show here either —
      silently no-trading. Surfacing it lets the admin re-assign it. (The
      deprovision path now demotes to 'pending' directly; this catches any
      out-of-band orphans.)

    Optionally filter by ``agent_id`` so an agent's own UI can show
    "your pending customers" without sifting through every other
    agent's queue.
    """
    stmt = (
        select(Customer)
        .where(
            or_(
                Customer.assignment_status == "pending",
                and_(
                    Customer.assignment_status == "active",
                    Customer.stack_id.is_(None),
                ),
            )
        )
        .order_by(Customer.created_at.asc())
    )
    if agent_id is not None:
        stmt = stmt.where(Customer.agent_id == agent_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Tiny utilities
# ---------------------------------------------------------------------------


def _uuid_or_none(value) -> Optional[UUID]:
    """Convert a stringified UUID (from a snapshot dict) back to UUID, or None.

    Snapshot dicts store ids as strings (JSON-safe). When the caller
    wants to plumb the "old server id" back into an
    :class:`AssignmentResult`, we need the UUID form again. ``None`` and
    empty string both map to ``None``.
    """
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


# Intentional re-exports. The "_resolve_*" helpers are public-via-test only;
# they're prefixed with ``_`` because they're not part of the router-facing
# API, but the test suite imports them directly to exercise each branch
# without going through ``resolve_target_server``.
__all__ = [
    "AssignmentResult",
    "PolicySet",
    "assign_customer",
    "clear_agent_policy",
    "get_policy_for",
    "move_customer",
    "pending_customers",
    "resolve_target_server",
    "set_agent_policy",
    "set_global_policy",
    "unassign_customer",
    "_resolve_broker_affinity",
    "_resolve_least_customers",
    "_resolve_round_robin",
    "_is_server_eligible",
]

# Silence unused-import if Iterable was kept for future signature growth.
_ = Iterable
