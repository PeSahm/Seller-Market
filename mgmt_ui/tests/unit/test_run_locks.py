"""Unit tests for the Phase-6 stack-level run lock service.

Pins the four "interesting" behaviours of
:mod:`app.services.run_locks`:

1. First acquire on an empty ``stack_run_locks`` row: inserts and
   returns a fresh lock.
2. Second acquire while the existing lock is still in its lease window:
   raises :class:`StackRunLockBusyError` carrying the prior holder and
   lease expiry so the route layer can render a 409 with useful detail.
3. Acquire when the prior row's lease has expired: logs a warning,
   deletes the stale row, inserts a fresh one. This is the
   crash-recovery path — without it a dead mgmt process would pin a
   stack forever.
4. ``release_lock`` deletes by the COMPOUND key ``(stack_id, run_id)``,
   not by ``stack_id`` alone — so a slow release that arrives after
   another holder has reclaimed the stale row is a harmless no-op
   instead of silently stealing the newcomer's lock.

A fifth pin guards the lease-window default (600 s) — the manual-run
lease is deliberately tied to the bot's subprocess timeout in
``SellerMarket/scheduler.py:227`` and silent drift would let a manual
run outlive the equivalent scheduled one.

No live DB — the :class:`sqlalchemy.ext.asyncio.AsyncSession` is faked
with :class:`unittest.mock.MagicMock` / :class:`AsyncMock` in the same
style as :mod:`tests.unit.test_stacks_push` and
:mod:`tests.unit.test_scheduler_jobs`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import run_locks as run_locks_svc
from app.services.run_locks import (
    DEFAULT_LEASE_SECONDS,
    StackRunLockBusyError,
    acquire_lock,
    release_lock,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _select_result(row) -> MagicMock:
    """Build the value returned by ``await db.execute(select(...))``.

    The acquire path calls ``.scalar_one_or_none()`` on the result. We
    return ``row`` (which may be ``None`` for the "empty table" case or a
    :class:`SimpleNamespace` for the "existing row" case).
    """
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=row)
    return result


def _make_db_with_execute_sequence(*results) -> MagicMock:
    """A MagicMock-shaped session whose ``execute`` returns each result in turn.

    The acquire path may call ``execute`` twice: once for the SELECT,
    optionally once for the stale-row DELETE. We don't care about the
    DELETE's return value — the second slot in the sequence is a generic
    MagicMock.
    """
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db


# ---------------------------------------------------------------------------
# 1. test_acquire_lock_inserts_when_empty
# ---------------------------------------------------------------------------


async def test_acquire_lock_inserts_when_empty() -> None:
    """Empty SELECT → ``db.add`` is called once with a fresh lock row.

    The returned :class:`StackRunLock` carries:

    * ``lease_expires_at`` strictly in the future (now + 600 s default).
    * ``holder`` and ``kind`` mirrored from the call kwargs.
    * ``run_id`` mirrored from the call kwargs — important because
      :func:`release_lock` uses it as half of its compound key.
    """
    stack_id = uuid.uuid4()
    run_id = uuid.uuid4()

    db = _make_db_with_execute_sequence(_select_result(None))

    before = datetime.now(timezone.utc)
    lock = await acquire_lock(
        db,
        stack_id=stack_id,
        run_id=run_id,
        kind="cache",
        holder="manual:test-actor",
    )
    after = datetime.now(timezone.utc)

    # One INSERT (via db.add) — no DELETE on the empty path.
    assert db.add.call_count == 1
    inserted = db.add.call_args.args[0]
    assert inserted is lock

    # The returned object reflects the kwargs verbatim.
    assert lock.stack_id == stack_id
    assert lock.run_id == run_id
    assert lock.kind == "cache"
    assert lock.holder == "manual:test-actor"

    # lease_expires_at is in the future — somewhere in the
    # [before+600s, after+600s] window depending on when the
    # implementation captured "now".
    expected_min = before + timedelta(seconds=DEFAULT_LEASE_SECONDS - 1)
    expected_max = after + timedelta(seconds=DEFAULT_LEASE_SECONDS + 1)
    assert expected_min <= lock.lease_expires_at <= expected_max

    # db.flush awaited after the insert.
    db.flush.assert_awaited()


# ---------------------------------------------------------------------------
# 2. test_acquire_lock_busy_raises_when_unexpired
# ---------------------------------------------------------------------------


async def test_acquire_lock_busy_raises_when_unexpired() -> None:
    """An existing row with ``lease_expires_at > now`` raises busy.

    The raised :class:`StackRunLockBusyError` MUST carry the existing
    holder string and lease-expiry timestamp so the route can render a
    useful 409 (and so an operator can decide whether to wait or kill
    the in-flight run).
    """
    stack_id = uuid.uuid4()
    run_id = uuid.uuid4()
    existing_expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    existing_row = SimpleNamespace(
        stack_id=stack_id,
        run_id=uuid.uuid4(),
        kind="cache",
        holder="manual:other-user",
        lease_expires_at=existing_expires,
    )

    db = _make_db_with_execute_sequence(_select_result(existing_row))

    with pytest.raises(StackRunLockBusyError) as exc_info:
        await acquire_lock(
            db,
            stack_id=stack_id,
            run_id=run_id,
            kind="cache",
            holder="manual:test-actor",
        )

    err = exc_info.value
    assert err.stack_id == stack_id
    assert err.holder == "manual:other-user"
    assert err.expires_at == existing_expires

    # Nothing new was inserted on the busy path.
    db.add.assert_not_called()


# ---------------------------------------------------------------------------
# 3. test_acquire_lock_reclaims_when_stale
# ---------------------------------------------------------------------------


async def test_acquire_lock_reclaims_when_stale(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stale row (``lease_expires_at < now``) is deleted then replaced.

    Behavioural expectations:

    * ``db.execute`` is awaited twice — once for the SELECT, once for
      the DELETE of the stale row.
    * ``db.add`` is called exactly once for the fresh row.
    * A WARNING-level log line is emitted (so silent reclaims don't
      mask a recurring crash pattern in production).
    """
    stack_id = uuid.uuid4()
    run_id = uuid.uuid4()
    stale_expires = datetime.now(timezone.utc) - timedelta(minutes=10)
    stale_row = SimpleNamespace(
        stack_id=stack_id,
        run_id=uuid.uuid4(),
        kind="cache",
        holder="manual:dead-process",
        lease_expires_at=stale_expires,
    )

    db = _make_db_with_execute_sequence(
        _select_result(stale_row),
        MagicMock(),  # DELETE result — unused
    )

    with caplog.at_level("WARNING", logger="app.services.run_locks"):
        lock = await acquire_lock(
            db,
            stack_id=stack_id,
            run_id=run_id,
            kind="trade",
            holder="manual:new-holder",
        )

    # SELECT + DELETE = two execute calls.
    assert db.execute.await_count == 2
    # Then the fresh INSERT.
    assert db.add.call_count == 1
    inserted = db.add.call_args.args[0]
    assert inserted is lock
    assert lock.holder == "manual:new-holder"
    assert lock.kind == "trade"
    assert lock.run_id == run_id
    assert lock.lease_expires_at > datetime.now(timezone.utc)

    # Warning logged with enough detail to alert on.
    assert any(
        "reclaiming stale stack_run_lock" in rec.message
        for rec in caplog.records
        if rec.levelname == "WARNING"
    ), "Expected WARNING about stale-lock reclamation"


# ---------------------------------------------------------------------------
# 4. test_release_lock_deletes_by_compound_key
# ---------------------------------------------------------------------------


async def test_release_lock_deletes_by_compound_key() -> None:
    """``release_lock`` filters by BOTH ``stack_id`` AND ``run_id``.

    This is the safety net for the "slow release after reclaim" race:
    if our run hung past the lease and someone else reclaimed it, our
    delayed release must NOT silently drop the newcomer's row.

    We assert by inspecting the SQL the implementation hands to
    ``db.execute`` — the rendered WHERE clause must reference both
    columns. Comparing the compiled WHERE string is fragile across
    SQLAlchemy versions but robust enough for "did we forget a clause"
    regressions; we use the SQLAlchemy ``DeleteStatementGenerator``
    introspection helpers via the statement's ``.whereclause`` attribute.
    """
    stack_id = uuid.uuid4()
    run_id = uuid.uuid4()

    db = MagicMock()
    db.execute = AsyncMock()

    await release_lock(db, stack_id=stack_id, run_id=run_id)

    db.execute.assert_awaited_once()
    stmt = db.execute.await_args.args[0]

    # Compile to string and assert both column references appear in the
    # WHERE clause. Using literal_binds so we don't have to inspect the
    # parameter dict.
    compiled = str(
        stmt.compile(compile_kwargs={"literal_binds": True})
    )
    assert "stack_run_locks.stack_id" in compiled, (
        "release_lock must filter on stack_id"
    )
    assert "stack_run_locks.run_id" in compiled, (
        "release_lock must ALSO filter on run_id — otherwise a slow "
        "release after a stale-lock reclaim would drop the newcomer's "
        "row, silently corrupting the per-stack mutex."
    )


# ---------------------------------------------------------------------------
# 5. test_default_lease_seconds_is_600
# ---------------------------------------------------------------------------


def test_default_lease_seconds_is_600() -> None:
    """The default lease window is pinned at 600 seconds.

    This matches the bot's subprocess timeout in
    ``SellerMarket/scheduler.py:227``. A silent change here would let a
    manual run outlive the equivalent scheduled one, breaking the
    "manual mirrors scheduled" invariant the lock relies on.
    """
    assert DEFAULT_LEASE_SECONDS == 600
