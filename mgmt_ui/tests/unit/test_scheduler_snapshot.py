"""Unit tests for the Phase-6 scheduler snapshot context manager.

:func:`app.services.scheduler_snapshot.disable_scheduler_for_stack` is
used by the manual-run path so the in-container scheduler doesn't fire
the same job while the operator is running it manually — which would
duplicate-order at the broker (run_trading) or stomp on the warm
session cache (cache_warmup).

The contract has five distinct branches; one test pins each:

1. All jobs already disabled → no DB write, no pusher call (skipping
   the SFTP write avoids needlessly grabbing the per-server compose
   lock).
2. At least one job enabled → DB bulk-disable, pusher called, snapshot
   yielded, then on context exit the snapshot is written back and the
   pusher is called a second time to re-deploy the original config.
3. An exception raised inside the with-block must NOT skip the restore
   — the ``finally`` is what guarantees the on-server file matches
   the DB once the run is done.
4. Pusher failure during the initial disable: the DB must be rolled
   back to the snapshot state and the original exception must
   propagate so the caller knows the disable didn't take.
5. Pusher failure during the restore is logged and swallowed (we don't
   want to mask any in-block exception with a downstream push error).

All tests use mocked sessions + jobs in the same style as
:mod:`tests.unit.test_scheduler_render_integration` and
:mod:`tests.unit.test_run_locks` — no live DB.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.scheduler_snapshot import disable_scheduler_for_stack


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_job(name: str, *, enabled: bool) -> SimpleNamespace:
    """Minimal :class:`SchedulerJob` stand-in.

    The snapshot reads ``.name`` and ``.enabled`` and uses ``.id`` to
    target the per-row UPDATE on restore. ``stack_id`` is unused by the
    snapshot itself but we set it for shape parity.
    """
    return SimpleNamespace(
        id=uuid.uuid4(),
        stack_id=uuid.uuid4(),
        name=name,
        enabled=enabled,
    )


def _select_result(rows: list) -> MagicMock:
    """Mock the ``await db.execute(select(SchedulerJob)...)`` chain.

    The snapshot does ``rows.scalars().all()`` on the result.
    """
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=rows)
    result.scalars = MagicMock(return_value=scalars)
    return result


def _make_db(jobs: list) -> MagicMock:
    """A session whose first ``execute`` returns the job list.

    Subsequent ``execute`` calls (UPDATE / disable-or-restore) get a
    plain MagicMock — the snapshot doesn't introspect their return
    values.
    """
    db = MagicMock()
    # First call returns the SELECT result; everything after is a
    # vanilla MagicMock so the AsyncMock doesn't choke on extra calls.
    db.execute = AsyncMock(
        side_effect=lambda *a, **kw: _next_execute(db, *a, **kw)
    )
    db._execute_calls = 0
    db._select_result = _select_result(jobs)
    db.commit = AsyncMock()
    return db


def _next_execute(db, *_args, **_kwargs):
    """Sequencing helper: first ``execute`` returns the SELECT result,
    every later one returns a generic MagicMock so UPDATEs don't blow
    up on attribute access.
    """
    db._execute_calls += 1
    if db._execute_calls == 1:
        return db._select_result
    return MagicMock()


# ---------------------------------------------------------------------------
# 1. test_noop_when_all_disabled
# ---------------------------------------------------------------------------


async def test_noop_when_all_disabled() -> None:
    """All jobs already disabled → pusher is NEVER called.

    The yielded snapshot is the verbatim ``{name: False}`` map so
    callers can still observe it (e.g. to log "no jobs to suppress"),
    but no SFTP write happens — we skip the per-server compose lock
    grab that the pusher would acquire.
    """
    stack_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    jobs = [
        _fake_job("cache_warmup", enabled=False),
        _fake_job("run_trading", enabled=False),
    ]
    db = _make_db(jobs)
    pusher = AsyncMock()

    async with disable_scheduler_for_stack(
        db, stack_id=stack_id, pusher=pusher, actor_id=actor_id
    ) as snapshot:
        assert snapshot == {"cache_warmup": False, "run_trading": False}

    pusher.assert_not_called()
    # Only the initial SELECT — no UPDATE, no second push.
    assert db._execute_calls == 1
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# 2. test_disable_pushes_and_restores_on_exit
# ---------------------------------------------------------------------------


async def test_disable_pushes_and_restores_on_exit() -> None:
    """One enabled job → pusher called twice (disable + restore).

    The snapshot reflects the pre-disable state and the restore writes
    each row back to the snapshotted ``enabled`` value. We verify by
    counting pusher calls and asserting the pusher receives the
    expected positional/keyword args on each.
    """
    stack_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    jobs = [
        _fake_job("cache_warmup", enabled=True),
        _fake_job("run_trading", enabled=False),
    ]
    db = _make_db(jobs)
    pusher = AsyncMock()

    async with disable_scheduler_for_stack(
        db, stack_id=stack_id, pusher=pusher, actor_id=actor_id
    ) as snapshot:
        # Snapshot reflects pre-disable state.
        assert snapshot == {"cache_warmup": True, "run_trading": False}

    # Pusher fired once on the way in, once on the way out.
    assert pusher.await_count == 2
    for call in pusher.await_args_list:
        # Each call: (db, stack_id, actor_id=actor_id).
        assert call.args[0] is db
        assert call.args[1] == stack_id
        assert call.kwargs.get("actor_id") == actor_id

    # commit() awaited at least twice (once after disable, once after
    # restore).
    assert db.commit.await_count >= 2


# ---------------------------------------------------------------------------
# 3. test_restore_runs_even_on_inblock_exception
# ---------------------------------------------------------------------------


async def test_restore_runs_even_on_inblock_exception() -> None:
    """An exception inside the with-block must still trigger restore.

    Without this, an SSH failure mid-run would leave the bot's
    scheduler permanently disabled — the operator would have to know
    to re-enable it manually. The ``finally`` is what guarantees the
    on-server file matches the DB once we're out the door.
    """
    stack_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    jobs = [_fake_job("cache_warmup", enabled=True)]
    db = _make_db(jobs)
    pusher = AsyncMock()

    class _InBlockBoom(RuntimeError):
        pass

    with pytest.raises(_InBlockBoom):
        async with disable_scheduler_for_stack(
            db, stack_id=stack_id, pusher=pusher, actor_id=actor_id
        ):
            raise _InBlockBoom("simulated SSH failure mid-run")

    # Pusher still called twice: disable + restore.
    assert pusher.await_count == 2


# ---------------------------------------------------------------------------
# 4. test_push_failure_during_disable_restores_db_and_reraises
# ---------------------------------------------------------------------------


async def test_push_failure_during_disable_restores_db_and_reraises() -> None:
    """Pusher raises during disable → DB rolled back, exception bubbles.

    If the disable write committed but the SFTP push failed, the DB
    would show ``enabled=False`` while the on-server file is the
    pre-disable version. The service detects this and rolls the DB
    rows back to the snapshot state, then re-raises so the caller
    knows the disable didn't take.

    Critically, the with-block body MUST NOT execute on this path —
    the snapshot context manager raises before yielding.
    """
    stack_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    jobs = [_fake_job("cache_warmup", enabled=True)]
    db = _make_db(jobs)

    class _PusherBoom(RuntimeError):
        pass

    push_calls = 0

    async def _exploding_pusher(*_args, **_kwargs):
        nonlocal push_calls
        push_calls += 1
        raise _PusherBoom("simulated SFTP failure")

    inblock_ran = False
    with pytest.raises(_PusherBoom):
        async with disable_scheduler_for_stack(
            db, stack_id=stack_id, pusher=_exploding_pusher, actor_id=actor_id
        ):
            inblock_ran = True  # pragma: no cover - must not execute

    assert inblock_ran is False, (
        "with-block must not run when the disable push raises"
    )
    assert push_calls == 1, "pusher should have been called exactly once"

    # The recovery path issued at least one extra UPDATE (per-row
    # restore) plus the initial UPDATE bulk-disable. We don't pin the
    # exact count because the implementation may legitimately collapse
    # them; we just want to see > 1 execute call (the initial SELECT
    # was call #1, the bulk-disable was #2, then per-row restores
    # follow).
    assert db._execute_calls >= 3
    # And a follow-up commit for the restore.
    assert db.commit.await_count >= 2


# ---------------------------------------------------------------------------
# 5. test_push_failure_during_restore_is_swallowed_with_log
# ---------------------------------------------------------------------------


async def test_push_failure_during_restore_is_swallowed_with_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pusher fails on restore → swallowed + logged, in-block work survives.

    By the time we're restoring we may already be unwinding from an
    in-block exception; masking it with a downstream push error would
    lose the original cause. The DB is restored (a strict invariant);
    only the on-server file is stale until the next admin save.
    """
    stack_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    jobs = [_fake_job("cache_warmup", enabled=True)]
    db = _make_db(jobs)

    push_calls = 0

    async def _pusher_fails_on_restore(*_args, **_kwargs):
        nonlocal push_calls
        push_calls += 1
        if push_calls == 1:
            return  # disable push succeeds
        raise RuntimeError("simulated SFTP failure on restore")

    inblock_done = False
    with caplog.at_level("ERROR", logger="app.services.scheduler_snapshot"):
        async with disable_scheduler_for_stack(
            db,
            stack_id=stack_id,
            pusher=_pusher_fails_on_restore,
            actor_id=actor_id,
        ):
            inblock_done = True

    # In-block code ran normally — the restore-push failure didn't
    # surface.
    assert inblock_done is True
    # Both pusher calls were attempted (disable + restore).
    assert push_calls == 2
    # And the failure was logged so operators can spot stale on-server
    # files in alerting / log review.
    assert any(
        "scheduler restore push failed" in rec.message
        for rec in caplog.records
    ), "Expected an ERROR log about restore-push failure"
