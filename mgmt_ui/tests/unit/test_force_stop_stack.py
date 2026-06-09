"""Unit tests for the force-kill service path.

Pins the behaviour of the three functions backing the "Force kill" /
"Force kill all" / "Run all" buttons:

* :func:`app.services.stacks.force_stop_stack` — ``docker compose stop -t 0``
  under the per-server advisory lock, then flip the row to ``down`` + audit.
* :func:`app.services.stacks.force_stop_stacks` — best-effort bulk wrapper.
* :func:`app.services.run_executor.run_all_stacks` — fan a manual run over
  many stacks, counting started / skipped (lock busy) / failed.

No real DB, no real SSH: the session is a ``MagicMock``; ``run_command`` is
faked via the module's ``_import_ssh_commands`` hook; the advisory-lock
helpers are monkey-patched (same harness as ``test_stacks_push.py``).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import run_executor, run_locks
from app.services import stacks as stacks_svc
from app.services.ssh.exceptions import SSHError


def _fake_stack(server_id: uuid.UUID | None = None, status: str = "up") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        server_id=server_id or uuid.uuid4(),
        agent_id=uuid.uuid4(),
        stack_dir="/root/seller-market/agents/abc",
        status=status,
        compose_project="sm-agent-abc",
    )


def _fake_server(server_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=server_id or uuid.uuid4(), base_dir="/root/seller-market/agents")


def _install_lock_session(monkeypatch, *, acquired: bool = True) -> dict:
    state = {"acquire_calls": [], "release_calls": []}

    @asynccontextmanager
    async def _fake_session_local():
        yield MagicMock()

    async def _fake_try_acquire(_session, key):
        state["acquire_calls"].append(key)
        return acquired

    async def _fake_release(_session, key):
        state["release_calls"].append(key)

    monkeypatch.setattr(stacks_svc, "AsyncSessionLocal", _fake_session_local)
    monkeypatch.setattr(stacks_svc, "try_acquire_session_lock", _fake_try_acquire)
    monkeypatch.setattr(stacks_svc, "release_session_lock", _fake_release)
    return state


def _install_compose_stop(monkeypatch, *, ok: bool = True, raises: Exception | None = None):
    """Patch the SSH ``run_command`` the ``_compose_stop`` helper resolves."""
    calls: list[str] = []

    async def _run_command(_server, cmd, **_kwargs):
        calls.append(cmd)
        if raises is not None:
            raise raises
        return SimpleNamespace(ok=ok, stdout="Stopped", stderr="")

    monkeypatch.setattr(stacks_svc, "_import_ssh_commands", lambda: _run_command)
    return calls


def _fake_db(server) -> MagicMock:
    db = MagicMock()
    db.get = AsyncMock(return_value=server)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    return db


# ---------------------------------------------------------------------------
# force_stop_stack
# ---------------------------------------------------------------------------


async def test_force_stop_marks_down_audits_and_uses_stop_t0(monkeypatch):
    server = _fake_server()
    stack = _fake_stack(server_id=server.id)
    db = _fake_db(server)
    monkeypatch.setattr(stacks_svc, "get_stack", AsyncMock(return_value=stack))
    state = _install_lock_session(monkeypatch, acquired=True)
    cmds = _install_compose_stop(monkeypatch, ok=True)

    result = await stacks_svc.force_stop_stack(db, stack.id, actor_id=None)

    assert result.ok is True
    assert result.status == "down"
    assert stack.status == "down"
    # The command is a project-scoped, immediate stop (compose_project is
    # shell-quoted, so match the bare token rather than the ``-p `` prefix).
    assert len(cmds) == 1
    assert "stop -t 0" in cmds[0]
    assert "sm-agent-abc" in cmds[0]
    # Audited + committed; lock acquired and released exactly once.
    db.add.assert_called_once()
    db.commit.assert_awaited()
    assert len(state["acquire_calls"]) == 1
    assert state["acquire_calls"] == state["release_calls"]


async def test_force_stop_nonzero_exit_still_marks_down(monkeypatch):
    # A non-zero compose exit (e.g. "no such service") still means the intent
    # is "down" — we mark the row down but report ok=False so the UI shows the
    # log tail.
    server = _fake_server()
    stack = _fake_stack(server_id=server.id)
    db = _fake_db(server)
    monkeypatch.setattr(stacks_svc, "get_stack", AsyncMock(return_value=stack))
    _install_lock_session(monkeypatch, acquired=True)
    _install_compose_stop(monkeypatch, ok=False)

    result = await stacks_svc.force_stop_stack(db, stack.id, actor_id=None)

    assert result.ok is False
    assert result.status == "down"
    assert stack.status == "down"
    assert "non-zero" in result.message
    db.commit.assert_awaited()


async def test_force_stop_lock_busy_raises_and_skips_ssh(monkeypatch):
    server = _fake_server()
    stack = _fake_stack(server_id=server.id)
    db = _fake_db(server)
    monkeypatch.setattr(stacks_svc, "get_stack", AsyncMock(return_value=stack))
    _install_lock_session(monkeypatch, acquired=False)

    def _no_ssh():
        raise AssertionError("compose must not run when the lock is busy")

    monkeypatch.setattr(stacks_svc, "_import_ssh_commands", _no_ssh)

    with pytest.raises(RuntimeError):
        await stacks_svc.force_stop_stack(db, stack.id, actor_id=None)
    # Row untouched, nothing committed.
    assert stack.status == "up"
    db.commit.assert_not_awaited()


async def test_force_stop_ssh_error_propagates_row_unchanged(monkeypatch):
    # Host unreachable: we must NOT claim the stack is down.
    server = _fake_server()
    stack = _fake_stack(server_id=server.id)
    db = _fake_db(server)
    monkeypatch.setattr(stacks_svc, "get_stack", AsyncMock(return_value=stack))
    state = _install_lock_session(monkeypatch, acquired=True)
    _install_compose_stop(monkeypatch, raises=SSHError("host down"))

    with pytest.raises(SSHError):
        await stacks_svc.force_stop_stack(db, stack.id, actor_id=None)

    assert stack.status == "up"          # left as-is
    db.commit.assert_not_awaited()
    # Lock still released on the exception path.
    assert len(state["release_calls"]) == 1


async def test_force_stop_unknown_stack_raises_lookuperror(monkeypatch):
    db = _fake_db(_fake_server())
    monkeypatch.setattr(stacks_svc, "get_stack", AsyncMock(return_value=None))
    with pytest.raises(LookupError):
        await stacks_svc.force_stop_stack(db, uuid.uuid4(), actor_id=None)


# ---------------------------------------------------------------------------
# force_stop_stacks (bulk, best-effort)
# ---------------------------------------------------------------------------


async def test_force_stop_stacks_best_effort_continues_past_failure(monkeypatch):
    ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    db = _fake_db(_fake_server())

    async def _fake_force_stop(_db, sid, *, actor_id):
        if sid == ids[1]:
            raise SSHError("host down")
        return SimpleNamespace(ok=True, stack_id=sid, status="down", message="", log_tail="")

    monkeypatch.setattr(stacks_svc, "force_stop_stack", _fake_force_stop)

    results = await stacks_svc.force_stop_stacks(db, ids, actor_id=None)

    assert len(results) == 3
    assert [r.ok for r in results] == [True, False, True]
    # The middle failure was captured (not raised) and the session reset.
    assert results[1].stack_id == ids[1]
    assert "force-kill failed" in results[1].message
    db.rollback.assert_awaited()


# ---------------------------------------------------------------------------
# run_all_stacks
# ---------------------------------------------------------------------------


async def test_run_all_stacks_counts_started_skipped_failed(monkeypatch):
    stacks = [_fake_stack() for _ in range(4)]

    async def _fake_start(*, stack_id, agent_id, job_name, actor_id):
        if stack_id == stacks[1].id:
            raise run_locks.StackRunLockBusyError(
                stack_id, holder="x", expires_at=datetime.now(timezone.utc)
            )
        if stack_id == stacks[2].id:
            raise RuntimeError("boom")
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(run_executor, "start_manual_run", _fake_start)

    started, skipped, failed = await run_executor.run_all_stacks(
        stacks, job_name="run_trading", actor_id=None
    )
    assert (started, skipped, failed) == (2, 1, 1)


async def test_run_all_stacks_empty_is_zero(monkeypatch):
    # Defensive: a guard so an empty list never calls the executor.
    monkeypatch.setattr(
        run_executor, "start_manual_run",
        AsyncMock(side_effect=AssertionError("must not run")),
    )
    assert await run_executor.run_all_stacks(
        [], job_name="cache_warmup", actor_id=None
    ) == (0, 0, 0)
