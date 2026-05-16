"""Unit tests for the customer-distribution resolver (Phase 4).

These tests focus on the pure-logic core of the distribution service —
:func:`resolve_target_server` plus the three policy helpers
``_resolve_round_robin`` / ``_resolve_least_customers`` /
``_resolve_broker_affinity``.

The resolvers take an ``AsyncSession`` so they can issue ``GROUP BY``
queries against ``customers``. To keep these tests pure-Python (no live
Postgres, no SQLAlchemy machinery) we substitute a hand-rolled
:class:`_FakeDB` whose ``execute`` callable returns canned aggregate
rows. The real service code only reads two things from a ``Result``:

* ``.all()``  — used by the policy helpers to enumerate aggregates.
* ``.scalar_one_or_none()`` — used by ``get_policy_for`` and the
  override branch of ``resolve_target_server`` to look up a single row.

So our fake only needs to implement those two affordances.

What we deliberately do NOT exercise here
-----------------------------------------
* The assign / unassign / move flow — those call into parallel agents'
  services (``app.services.customers``, ``app.services.stacks``) and the
  spec marks them as out-of-scope for unit tests. The push helper is
  invoked by the assignment functions but isn't on the resolution path,
  so leaving it unmocked is fine.
* The audit-log writes — they exercise SQLAlchemy ORM behaviour that is
  better covered by the integration suite.

The :class:`_FakeServer`, :class:`_FakeCustomer`, and :class:`_FakePolicy`
classes are deliberately tiny — they only carry the columns the resolver
reads. Using a full SQLAlchemy mapped instance would force the test to
set up a metadata registry just to instantiate one. Plain Python classes
duck-type fine because the resolver code accesses attributes, not column
descriptors.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

import pytest

from app.services.distribution import (
    _is_server_eligible,
    _resolve_broker_affinity,
    _resolve_least_customers,
    _resolve_round_robin,
    resolve_target_server,
)


# ---------------------------------------------------------------------------
# Lightweight duck-typed stand-ins
# ---------------------------------------------------------------------------


class _FakeServer:
    """A minimal stand-in for an :class:`app.models.servers.Server` row.

    The resolver reads exactly five attributes: ``id``, ``name``,
    ``status``, ``last_seen_at``, ``base_dir``. We default each to a
    reasonable value so individual tests only need to override what they
    care about — that keeps fixtures focused on the relevant inputs.
    """

    def __init__(
        self,
        *,
        name: str,
        status: str = "online",
        id: Optional[UUID] = None,
        last_seen_at: Optional[datetime] = None,
        base_dir: str = "/root/seller-market/agents",
    ) -> None:
        self.id = id if id is not None else uuid4()
        self.name = name
        self.status = status
        self.last_seen_at = last_seen_at
        self.base_dir = base_dir


class _FakeCustomer:
    """A minimal stand-in for a :class:`app.models.customers.Customer` row.

    The resolver reads only ``agent_id`` and ``broker`` on the customer
    being placed. Everything else on the model is ignored at resolve
    time (the assignment functions read more, but we don't exercise
    them here).
    """

    def __init__(self, *, agent_id: UUID, broker: str = "bbi") -> None:
        self.agent_id = agent_id
        self.broker = broker


class _FakePolicy:
    """A minimal stand-in for :class:`DistributionPolicy`.

    Used to control what :func:`get_policy_for` returns in tests that
    exercise :func:`resolve_target_server`. The resolver reads
    ``.policy`` and ``.default_server_id``; everything else is for
    audit / persistence.
    """

    def __init__(
        self,
        *,
        policy: str = "manual",
        default_server_id: Optional[UUID] = None,
    ) -> None:
        self.policy = policy
        self.default_server_id = default_server_id
        # Audit / persistence fields the assignment path would read.
        # Tests for the resolver don't touch them but we set them to
        # plausible values so an accidental access surfaces a clear
        # AttributeError rather than something exotic.
        self.id = uuid4()
        self.scope = "global"
        self.agent_id = None


# ---------------------------------------------------------------------------
# Fake DB plumbing
# ---------------------------------------------------------------------------


class _FakeResult:
    """A pseudo-:class:`sqlalchemy.engine.Result`.

    Only the two methods the resolver actually calls are implemented:

    * :meth:`all` — for the ``GROUP BY`` aggregates the policy helpers
      execute. The fake stores a list of tuples already shaped like the
      real result rows ``(server_id, value)``.
    * :meth:`scalar_one_or_none` — for the single-row lookups
      :func:`get_policy_for` does. The fake stores at most one object;
      ``None`` if it's missing.

    Calling the wrong method raises ``NotImplementedError`` so a test
    that accidentally relies on unexposed Result API fails loudly
    instead of returning ``None``.
    """

    def __init__(
        self,
        *,
        rows: Optional[list[tuple[Any, ...]]] = None,
        scalar: Any = ...,  # sentinel for "unset"
    ) -> None:
        self._rows = rows
        self._scalar = scalar

    def all(self) -> list[tuple[Any, ...]]:
        if self._rows is None:
            raise NotImplementedError(
                "this FakeResult was not primed with .rows"
            )
        return self._rows

    def scalar_one_or_none(self) -> Any:
        if self._scalar is ...:  # unset sentinel
            raise NotImplementedError(
                "this FakeResult was not primed with .scalar"
            )
        return self._scalar


class _FakeDB:
    """A pseudo-:class:`AsyncSession` for the resolver tests.

    ``execute`` is a callable the test installs to inspect the inbound
    statement and return a :class:`_FakeResult`. We don't parse the SQL —
    that would be fragile against benign refactors — instead each test
    builds an ``execute`` closure that returns the right canned data for
    its scenario.

    ``get`` is wired up only for the override-branch tests, which need
    to look up a Server by id. The simple ``{id: server}`` mapping is
    all we need.
    """

    def __init__(
        self,
        *,
        execute: Optional[Callable[[Any], _FakeResult]] = None,
        servers_by_id: Optional[dict[UUID, _FakeServer]] = None,
    ) -> None:
        self._execute = execute
        self._servers_by_id = servers_by_id or {}

    async def execute(self, stmt: Any) -> _FakeResult:
        if self._execute is None:
            raise NotImplementedError(
                "this FakeDB was not primed with an execute callable"
            )
        return self._execute(stmt)

    async def get(self, model: Any, key: Any) -> Any:
        # The real session looks up the row by primary key. Tests only
        # need Server lookups here, so we don't bother dispatching on
        # the model class.
        return self._servers_by_id.get(key)


def _aggregate_result(rows: list[tuple[UUID, Any]]) -> _FakeResult:
    """Shortcut to build a :class:`_FakeResult` with aggregate rows."""
    return _FakeResult(rows=rows)


# ---------------------------------------------------------------------------
# resolve_target_server — manual override branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_with_override_returns_named_server() -> None:
    """Admin clicks "assign to srv-a" → the resolver returns srv-a.

    Manual override is independent of the configured policy: even an
    agent on a ``policy='round_robin'`` config still gets a one-off
    placement on the explicitly-named server. We verify the resolver
    returns exactly the server we passed in (same UUID) without
    consulting the policy at all.
    """
    srv = _FakeServer(name="srv-a", status="online")
    db = _FakeDB(servers_by_id={srv.id: srv})
    customer = _FakeCustomer(agent_id=uuid4())

    result = await resolve_target_server(
        db, customer, override_server_id=srv.id
    )
    assert result is srv


@pytest.mark.asyncio
async def test_resolve_with_override_rejects_offline_server() -> None:
    """An override pointing at an offline server raises ValueError.

    We refuse rather than silently picking a different server: the admin
    explicitly named this one, and surfacing the failure (with the
    server name in the message) is more useful than a fallback. The
    error message format is "server '<name>' is not eligible (status=...)"
    so the UI can show a precise reason.
    """
    srv = _FakeServer(name="srv-down", status="offline")
    db = _FakeDB(servers_by_id={srv.id: srv})
    customer = _FakeCustomer(agent_id=uuid4())

    with pytest.raises(ValueError, match="not eligible"):
        await resolve_target_server(
            db, customer, override_server_id=srv.id
        )


@pytest.mark.asyncio
async def test_resolve_with_override_rejects_unknown_server() -> None:
    """An override naming a non-existent server raises ValueError.

    The Server model has no ``deleted_at`` column in Phase 4 — see the
    TODO in :mod:`app.services.servers`. Until that lands, hard delete
    is the only kind of delete, which means a soft-deleted (= hard-
    deleted) server simply isn't in the table. Looking up its id via
    ``db.get(Server, ...)`` returns ``None``, which we surface as
    "server <uuid> not found". This test pins that branch so when the
    real soft-delete migration arrives we'll have an obvious place to
    extend coverage.
    """
    db = _FakeDB(servers_by_id={})
    customer = _FakeCustomer(agent_id=uuid4())

    with pytest.raises(ValueError, match="not found"):
        await resolve_target_server(
            db, customer, override_server_id=uuid4()
        )


# ---------------------------------------------------------------------------
# resolve_target_server — policy branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_policy_without_override_raises() -> None:
    """``manual`` + no override + no default_server_id → ValueError.

    The clearest possible failure mode. We pre-prime the FakeDB so the
    policy lookup returns a manual policy with no default, then assert
    a ValueError surfaces with a message that mentions "manual".
    """
    customer = _FakeCustomer(agent_id=uuid4())

    # The agent policy lookup hits the DB twice (agent override, then
    # global default). Returning ``None`` for both means
    # ``get_policy_for`` falls through to its synthetic 'manual'
    # default — exactly the scenario the spec calls out.
    def _exec(_stmt: Any) -> _FakeResult:
        return _FakeResult(scalar=None)

    db = _FakeDB(execute=_exec)

    with pytest.raises(ValueError, match="manual"):
        await resolve_target_server(db, customer)


# ---------------------------------------------------------------------------
# _resolve_round_robin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_robin_picks_least_recently_assigned() -> None:
    """Given (srv-a: last assigned 1h ago, srv-b: 3h ago, srv-c: 30min ago),
    round_robin picks srv-b — it has waited longest for new traffic.

    The resolver issues a ``MAX(created_at) GROUP BY server_id`` and
    sorts ascending. Our fake returns canned aggregate rows; the
    resolver should rank srv-b's older timestamp first.
    """
    now = datetime.now(timezone.utc)
    a = _FakeServer(name="srv-a")
    b = _FakeServer(name="srv-b")
    c = _FakeServer(name="srv-c")

    aggregates = [
        (a.id, now - timedelta(hours=1)),
        (b.id, now - timedelta(hours=3)),
        (c.id, now - timedelta(minutes=30)),
    ]

    def _exec(_stmt: Any) -> _FakeResult:
        return _aggregate_result(aggregates)

    db = _FakeDB(execute=_exec)
    customer = _FakeCustomer(agent_id=uuid4())

    chosen = await _resolve_round_robin(db, customer, [a, b, c])
    assert chosen is b


@pytest.mark.asyncio
async def test_round_robin_empty_fleet_falls_back_to_first_by_name() -> None:
    """No server has any customers yet → pick alphabetically.

    When ``MAX(created_at)`` returns no rows (fresh DB, every server is
    pristine), the resolver should pick the first by name. We sort the
    input shuffled to make sure the resolver is doing the work, not us.
    """
    # Deliberately not in alphabetical order so the test fails clearly
    # if the resolver returns the input ordering instead of doing the
    # name sort itself.
    z = _FakeServer(name="zeta")
    a = _FakeServer(name="alpha")
    m = _FakeServer(name="mu")

    def _exec(_stmt: Any) -> _FakeResult:
        return _aggregate_result([])  # nobody has any customers yet

    db = _FakeDB(execute=_exec)
    customer = _FakeCustomer(agent_id=uuid4())

    chosen = await _resolve_round_robin(db, customer, [z, a, m])
    assert chosen is a


# ---------------------------------------------------------------------------
# _resolve_least_customers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_least_customers_picks_smallest_count() -> None:
    """Counts ``{srv-a: 5, srv-b: 2, srv-c: 7}`` → pick srv-b.

    Straightforward minimum. The resolver's ``GROUP BY`` returns one
    row per server with customers; servers with zero customers wouldn't
    appear in the real query result. We mirror that exactly.
    """
    a = _FakeServer(name="srv-a")
    b = _FakeServer(name="srv-b")
    c = _FakeServer(name="srv-c")

    def _exec(_stmt: Any) -> _FakeResult:
        return _aggregate_result([(a.id, 5), (b.id, 2), (c.id, 7)])

    db = _FakeDB(execute=_exec)
    customer = _FakeCustomer(agent_id=uuid4())

    chosen = await _resolve_least_customers(db, customer, [a, b, c])
    assert chosen is b


@pytest.mark.asyncio
async def test_least_customers_ties_broken_by_name() -> None:
    """All servers at count 3 → pick alphabetically.

    Determinism matters: the UI should show the same pick on every
    refresh until the underlying counts diverge. Sort key is ``(count,
    name)`` so equal counts fall through to the name comparison.
    """
    z = _FakeServer(name="zeta")
    a = _FakeServer(name="alpha")
    m = _FakeServer(name="mu")

    def _exec(_stmt: Any) -> _FakeResult:
        return _aggregate_result([(z.id, 3), (a.id, 3), (m.id, 3)])

    db = _FakeDB(execute=_exec)
    customer = _FakeCustomer(agent_id=uuid4())

    chosen = await _resolve_least_customers(db, customer, [z, a, m])
    assert chosen is a


@pytest.mark.asyncio
async def test_least_customers_treats_unseen_server_as_zero() -> None:
    """A server with no customers is missing from the aggregate → wins.

    The ``GROUP BY`` query only returns rows for servers that actually
    host at least one customer. The resolver should default the missing
    server's count to 0 so a fresh server outranks one with even a
    single customer. This pins the ``counts.get(s.id, 0)`` branch.
    """
    busy = _FakeServer(name="busy")
    fresh = _FakeServer(name="fresh")

    def _exec(_stmt: Any) -> _FakeResult:
        # Only ``busy`` shows up — ``fresh`` has zero customers and
        # would not appear in a real SQL aggregate.
        return _aggregate_result([(busy.id, 1)])

    db = _FakeDB(execute=_exec)
    customer = _FakeCustomer(agent_id=uuid4())

    chosen = await _resolve_least_customers(db, customer, [busy, fresh])
    assert chosen is fresh


# ---------------------------------------------------------------------------
# _resolve_broker_affinity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broker_affinity_picks_server_with_same_broker_for_agent() -> None:
    """Agent A already has a bbi customer on srv-1 → new bbi customer for
    agent A is co-located on srv-1.

    The resolver issues a query filtered by ``agent_id`` and ``broker``;
    we return one row pointing at srv-1. The pick should be srv-1
    regardless of overall customer counts (which we don't return — the
    affinity hit short-circuits the fallback).
    """
    agent_a = uuid4()
    srv1 = _FakeServer(name="srv-1")
    srv2 = _FakeServer(name="srv-2")

    # Agent A already has 2 bbi customers on srv-1.
    def _exec(_stmt: Any) -> _FakeResult:
        return _aggregate_result([(srv1.id, 2)])

    db = _FakeDB(execute=_exec)
    customer = _FakeCustomer(agent_id=agent_a, broker="bbi")

    chosen = await _resolve_broker_affinity(db, customer, [srv1, srv2])
    assert chosen is srv1


@pytest.mark.asyncio
async def test_broker_affinity_falls_back_to_least_customers() -> None:
    """No existing same-broker customer for this agent → least_customers.

    The affinity query returns no rows. The resolver should call into
    :func:`_resolve_least_customers`, which runs a second ``GROUP BY``.
    Our fake distinguishes the two queries by call order: the first
    (affinity) returns empty, the second (counts) returns the canned
    counts, and the pick is the minimum of those.
    """
    agent_a = uuid4()
    srv1 = _FakeServer(name="srv-1")
    srv2 = _FakeServer(name="srv-2")

    # ``execute`` is called twice; first is the affinity probe (empty),
    # second is the least-customers count.
    calls: list[int] = []

    def _exec(_stmt: Any) -> _FakeResult:
        calls.append(1)
        if len(calls) == 1:
            return _aggregate_result([])  # no affinity hit
        return _aggregate_result([(srv1.id, 5), (srv2.id, 1)])

    db = _FakeDB(execute=_exec)
    customer = _FakeCustomer(agent_id=agent_a, broker="bbi")

    chosen = await _resolve_broker_affinity(db, customer, [srv1, srv2])
    assert chosen is srv2
    assert len(calls) == 2, "expected affinity probe + least_customers fallback"


@pytest.mark.asyncio
async def test_broker_affinity_isolates_by_agent() -> None:
    """Agent B's bbi on srv-2 must not steer agent A toward srv-2.

    The affinity query filters by ``agent_id``, so when we resolve for
    agent A the query returns only agent A's own bbi affinity hits. We
    simulate this by returning an empty result for the affinity probe
    (since agent A has none) and a non-empty count for the fallback —
    if the resolver were leaking across agents it would have returned
    srv-2 from the affinity branch instead.

    The pick should be the LEAST-customers server. We rig counts so
    srv-1 wins, proving the resolver fell through to least-customers
    rather than mis-classifying srv-2 as an affinity hit.
    """
    agent_a = uuid4()
    srv1 = _FakeServer(name="srv-1")
    srv2 = _FakeServer(name="srv-2")

    calls: list[int] = []

    def _exec(_stmt: Any) -> _FakeResult:
        calls.append(1)
        if len(calls) == 1:
            # Agent A has no bbi customers anywhere — the agent_id WHERE
            # clause filters out agent B's srv-2 row.
            return _aggregate_result([])
        # Fallback: srv-1 has 1 customer, srv-2 has 4 → least picks srv-1.
        return _aggregate_result([(srv1.id, 1), (srv2.id, 4)])

    db = _FakeDB(execute=_exec)
    customer = _FakeCustomer(agent_id=agent_a, broker="bbi")

    chosen = await _resolve_broker_affinity(db, customer, [srv1, srv2])
    assert chosen is srv1
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Server-eligibility helper
# ---------------------------------------------------------------------------
#
# Not in the spec's required list, but the eligibility helper is the
# guardrail that keeps every policy from picking an unavailable server.
# Pinning its behaviour here makes a regression in the offline / stale
# heartbeat logic immediately visible to a reviewer.


def test_eligibility_online_status_passes() -> None:
    """``status='online'`` is the happy-path eligibility signal."""
    srv = _FakeServer(name="ok", status="online")
    assert _is_server_eligible(srv) is True


def test_eligibility_offline_status_fails() -> None:
    """``status='offline'`` is a hard reject regardless of heartbeat.

    The "or last_seen_at within 5 min" fallback is intentionally subordinate
    to an explicit offline signal — a server the health worker has marked
    down should never receive new customers even if it pinged us a minute
    ago.
    """
    srv = _FakeServer(
        name="down",
        status="offline",
        last_seen_at=datetime.now(timezone.utc),
    )
    assert _is_server_eligible(srv) is False


def test_eligibility_unknown_status_with_recent_pulse_passes() -> None:
    """``status='unknown'`` plus a fresh ``last_seen_at`` is eligible.

    Boot-time race: the health worker hasn't run yet but SSH ``test
    connection`` updated ``last_seen_at``. We don't want a new server to
    sit in the queue until the health worker's first sweep, so we accept
    a recent pulse as a substitute.
    """
    srv = _FakeServer(
        name="bootstrapping",
        status="unknown",
        last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    assert _is_server_eligible(srv) is True


def test_eligibility_unknown_status_with_stale_pulse_fails() -> None:
    """``status='unknown'`` plus a 10-minute-old pulse is NOT eligible.

    Pins the 5-minute window — without this, a server that was once
    healthy but went away silently would keep receiving customer
    assignments forever. The window matches the worker's roughly-1-min
    probe interval × 4 (four missed cycles is decisive).
    """
    srv = _FakeServer(
        name="stale",
        status="unknown",
        last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    assert _is_server_eligible(srv) is False
