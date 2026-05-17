"""Unit tests for :func:`app.services.audit.list_audit` (Phase 9).

The DB layer is mocked entirely — these tests assert pure filtering
behaviour by inspecting the compiled SQL of the SELECT statement that
``list_audit`` issues against the mock session. We don't run anything
against a real Postgres; the goal is to pin the WHERE-clause shape so a
future refactor that accidentally drops a filter (e.g. converting an
``if x is not None`` to ``if x:`` and losing the explicit-None branch)
fails the suite.

We pin six behaviours below:

* Each filter (``actor_id``, ``action``, ``target_type``, ``target_id``,
  ``since``, ``until``) ends up as a WHERE clause in the SQL.
* The ``actions`` multi-select produces an ``IN (...)`` clause.
* ``actions`` overrides ``action`` when both are passed.
* An empty ``actions`` iterable yields NO action filter at all
  (not "no rows").
* ``limit`` is applied as a LIMIT in the SQL.
* The ORDER BY is ``ts DESC``.

We don't bother testing :func:`get_audit` here — it's a one-line
``db.get`` and any breakage would surface in the integration tests
that hit the real router.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.audit import list_audit


# ---------------------------------------------------------------------------
# Fake AsyncSession
# ---------------------------------------------------------------------------


class _FakeDB:
    """Minimal :class:`AsyncSession` stand-in for ``list_audit``.

    Records every statement passed to :meth:`execute` so the test can
    compile it back to SQL and assert on the WHERE / ORDER BY / LIMIT
    shape. ``scalars().all()`` always returns an empty list since these
    tests are about the query construction, not the row projection.
    """

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)
        result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[])
        result.scalars = MagicMock(return_value=scalars)
        return result


def _compile_sql(stmt: Any) -> str:
    """Compile a SQLAlchemy ``select`` to its SQL string (lowercased)."""
    compiled = stmt.compile(compile_kwargs={"literal_binds": False})
    return str(compiled).lower()


def _bound_params(stmt: Any) -> dict[str, Any]:
    """Return the bound-param dict for a compiled statement."""
    return dict(stmt.compile().params)


# ---------------------------------------------------------------------------
# 1. Every filter shows up as a WHERE clause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_audit_each_filter_adds_where_clause() -> None:
    """Pass every filter; confirm each one ends up in the WHERE."""
    db = _FakeDB()

    actor = uuid4()
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    until = datetime(2026, 2, 1, tzinfo=timezone.utc)

    await list_audit(
        db,  # type: ignore[arg-type]
        actor_id=actor,
        action="customer.create",
        target_type="customer",
        target_id="abc-123",
        since=since,
        until=until,
        limit=50,
    )

    assert len(db.statements) == 1
    sql = _compile_sql(db.statements[0])
    binds = _bound_params(db.statements[0])

    # Column references on the SELECT side AND in the WHERE clauses.
    assert "audit_log" in sql
    assert "actor_user_id" in sql
    assert "action" in sql
    assert "target_type" in sql
    assert "target_id" in sql
    assert "ts" in sql
    # The compiled SQL has the WHERE bind names; we check the bound
    # values landed in the params dict.
    bind_values = list(binds.values())
    assert actor in bind_values
    assert "customer.create" in bind_values
    assert "customer" in bind_values
    assert "abc-123" in bind_values
    assert since in bind_values
    assert until in bind_values


# ---------------------------------------------------------------------------
# 2. actions multi-select -> IN (...)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_audit_actions_multi_select_uses_in_clause() -> None:
    """``actions=[...]`` compiles to ``action IN (...)``, not equality."""
    db = _FakeDB()
    await list_audit(
        db,  # type: ignore[arg-type]
        actions=["customer.create", "customer.update", "customer.delete"],
    )
    sql = _compile_sql(db.statements[0])
    # SQLAlchemy renders IN-clauses with the placeholder form ``in_``
    # or as expanded bind params; both contain the word "in" between
    # the column and the parens. We accept either spelling by checking
    # for the substring ``action in`` (with a space) which catches
    # both ``action IN (...)`` and ``audit_log.action IN (...)``.
    assert " in (" in sql or " in(" in sql
    assert "action" in sql
    binds = _bound_params(db.statements[0])
    bind_values = list(binds.values())
    # All three actions should appear among the bound params (either
    # as a list under one key for "expanding" bind, or split into three
    # individual binds — accept either shape).
    flat: list = []
    for v in bind_values:
        if isinstance(v, (list, tuple)):
            flat.extend(v)
        else:
            flat.append(v)
    assert "customer.create" in flat
    assert "customer.update" in flat
    assert "customer.delete" in flat


# ---------------------------------------------------------------------------
# 3. actions overrides action when both given
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_audit_actions_overrides_single_action() -> None:
    """When both ``action`` and ``actions`` are passed, ``actions`` wins.

    Matches the UI's behaviour where selecting a chip in the
    multi-select overrides any pre-existing single-value selection.
    The single-value ``action`` MUST NOT also be ANDed in — that would
    produce zero results if the single ``action`` isn't in the
    multi-select set, surprising the operator.
    """
    db = _FakeDB()
    await list_audit(
        db,  # type: ignore[arg-type]
        action="single.action",  # should be IGNORED
        actions=["multi.a", "multi.b"],
    )
    binds = _bound_params(db.statements[0])
    bind_values: list = []
    for v in binds.values():
        if isinstance(v, (list, tuple)):
            bind_values.extend(v)
        else:
            bind_values.append(v)

    assert "multi.a" in bind_values
    assert "multi.b" in bind_values
    # The single-value action must NOT appear as a bind value at all
    # — that would mean it was ANDed in alongside the IN clause.
    assert "single.action" not in bind_values


# ---------------------------------------------------------------------------
# 4. Empty actions iterable -> no filter (not "no rows")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_audit_empty_actions_iterable_means_no_filter() -> None:
    """``actions=[]`` is treated as "no filter on action", not "no rows".

    The multi-select form on the UI yields an empty list when the user
    hasn't picked anything — and the expected behaviour is "show me
    everything", not "show me nothing". A naïve
    ``if actions: ... else: return []`` would break this; explicit
    pinning here prevents the regression.
    """
    db = _FakeDB()
    await list_audit(
        db,  # type: ignore[arg-type]
        actions=[],
    )
    sql = _compile_sql(db.statements[0])
    binds = _bound_params(db.statements[0])
    # No action filter at all -> no equality bind, no IN bind. The only
    # binds should be the LIMIT.
    bind_values: list = []
    for v in binds.values():
        if isinstance(v, (list, tuple)):
            bind_values.extend(v)
        else:
            bind_values.append(v)
    # Nothing in the SELECT-from-audit_log shape implies a WHERE on
    # action: SQL should NOT contain a literal ``action =`` or
    # ``action in`` clause-wise.
    assert "where" not in sql, (
        f"empty actions list must not produce a WHERE clause; got SQL: {sql}"
    )


# ---------------------------------------------------------------------------
# 5. limit parameter applied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_audit_limit_applied() -> None:
    """The ``limit`` kwarg lands as a LIMIT in the compiled SQL."""
    db = _FakeDB()
    await list_audit(db, limit=42)  # type: ignore[arg-type]
    sql = _compile_sql(db.statements[0])
    assert "limit" in sql
    # And the default differs — verify by passing a non-default value.
    binds = _bound_params(db.statements[0])
    assert 42 in binds.values()


# ---------------------------------------------------------------------------
# 6. ORDER BY ts DESC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_audit_orders_by_ts_desc() -> None:
    """Rows MUST come back newest-first.

    Pins the ``order_by(desc(AuditLog.ts))`` — a regression to
    ``asc()`` (or no order at all) would silently push the recent
    audit entries off the bottom of the operator's table.
    """
    db = _FakeDB()
    await list_audit(db)  # type: ignore[arg-type]
    sql = _compile_sql(db.statements[0])
    assert "order by" in sql
    # SQLAlchemy renders desc as ``ts desc``. Strip whitespace
    # variations.
    normalised = " ".join(sql.split())
    assert "order by audit_log.ts desc" in normalised, (
        f"expected ORDER BY ts DESC; got: {normalised}"
    )


# ---------------------------------------------------------------------------
# 7. action_contains substring filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_contains_compiles_to_ilike_in_sql() -> None:
    """``action_contains=customer`` produces an ILIKE in the SQL WHERE.

    Pins the fix for PR #55's "Apply ``action`` filtering before the
    SQL LIMIT" finding — a Python-side post-filter was silently
    dropping matching rows that fell outside the 300-row cap window.
    """
    db = _FakeDB()
    await list_audit(db, action_contains="customer")  # type: ignore[arg-type]
    sql = _compile_sql(db.statements[0]).lower()
    assert "like" in sql, f"expected ILIKE in SQL; got: {sql}"
    binds = _bound_params(db.statements[0])
    # The pattern is wrapped with %...% by the helper.
    assert any(
        isinstance(v, str) and v.startswith("%") and v.endswith("%") and "customer" in v
        for v in binds.values()
    ), f"expected a '%customer%' bind value; got: {list(binds.values())}"


@pytest.mark.asyncio
async def test_action_contains_escapes_like_wildcards() -> None:
    """Literal ``_`` / ``%`` in the search term must be escaped.

    A naive ``ILIKE %term%`` with ``term="user_id"`` would match
    ``user-id``, ``userXid``, etc. — semantics drift. We escape the
    metacharacters so the user sees only literal substring matches.
    """
    db = _FakeDB()
    await list_audit(db, action_contains="user_id")  # type: ignore[arg-type]
    binds = _bound_params(db.statements[0])
    pattern = next(
        v for v in binds.values()
        if isinstance(v, str) and v.startswith("%")
    )
    # The literal underscore in the user's input is escaped.
    assert "\\_" in pattern, (
        f"expected the underscore to be escaped in {pattern!r}"
    )


@pytest.mark.asyncio
async def test_action_contains_empty_string_means_no_filter() -> None:
    """An empty/None ``action_contains`` should NOT add a WHERE clause.

    Falsy values come through the empty-string-tolerant query-param
    parsing in the route; they must not generate ``ILIKE '%%'`` which
    matches everything (and adds a useless DB-side scan).
    """
    db = _FakeDB()
    await list_audit(db, action_contains=None)  # type: ignore[arg-type]
    db2 = _FakeDB()
    await list_audit(db2, action_contains="")  # type: ignore[arg-type]
    for stmt in (db.statements[0], db2.statements[0]):
        sql = _compile_sql(stmt).lower()
        assert "like" not in sql, (
            f"empty action_contains must not produce LIKE; got: {sql}"
        )
