"""Unit tests for the Phase-5 scheduler / locust push + preview helpers.

These tests pin the behaviour of the four new public coroutines we added in
Phase 5:

* :func:`app.services.stacks.push_scheduler_config_for_stack` — render
  ``scheduler_config.json`` from current DB state and SFTP-push under the
  per-server advisory lock. Called by the scheduler editor route.
* :func:`app.services.stacks.push_locust_config_for_stack` — same shape,
  for ``locust_config.json``. Called by the locust editor route.
* :func:`app.services.stacks.render_scheduler_config_for_stack_preview` —
  ``(current_remote, new_rendered)`` tuple for the diff-preview UI.
* :func:`app.services.stacks.render_locust_config_for_stack_preview` —
  same for the locust preview.

The structure mirrors :mod:`tests.unit.test_stacks_push` (the equivalent
Phase-4 tests for ``push_config_ini_for_stack``). No real DB, no real SSH.
The SQLAlchemy session is faked with :class:`unittest.mock.MagicMock`; the
SFTP helpers are replaced via the lazy importer hook
(:func:`app.services.stacks._import_sftp`); the advisory-lock helpers are
monkey-patched.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import stacks as stacks_svc
from app.services.ssh.exceptions import SSHError


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _fake_stack(server_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        server_id=server_id or uuid.uuid4(),
        agent_id=uuid.uuid4(),
        stack_dir="/root/seller-market/agents/abc",
        status="up",
        compose_project="sm-agent-abc",
    )


def _fake_server(server_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=server_id or uuid.uuid4(),
        base_dir="/root/seller-market/agents",
    )


def _install_lock_session(
    monkeypatch: pytest.MonkeyPatch, *, acquired: bool = True
) -> dict:
    """Patch ``AsyncSessionLocal`` to return a no-op async context manager.

    Also patches the advisory-lock helpers. Returns a dict the caller can
    inspect:

    * ``acquire_calls`` — list of int lock keys passed to
      ``try_acquire_session_lock``.
    * ``release_calls`` — list of int lock keys passed to
      ``release_session_lock``.
    """
    state: dict = {
        "acquire_calls": [],
        "release_calls": [],
    }

    @asynccontextmanager
    async def _fake_session_local():
        yield MagicMock()

    async def _fake_try_acquire(_session, key):
        state["acquire_calls"].append(key)
        return acquired

    async def _fake_release(_session, key):
        state["release_calls"].append(key)

    monkeypatch.setattr(stacks_svc, "AsyncSessionLocal", _fake_session_local)
    monkeypatch.setattr(
        stacks_svc, "try_acquire_session_lock", _fake_try_acquire
    )
    monkeypatch.setattr(stacks_svc, "release_session_lock", _fake_release)
    return state


def _install_render_pipeline_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scheduler_rendered: str = '{"enabled": true, "jobs": []}\n',
    locust_rendered: str = '{"locust": {"users": 10}}\n',
) -> None:
    """Skip the DB-loading half of the render pipeline.

    Tests for the pipeline itself live elsewhere; here we just need
    ``_build_render_context`` plus the two JSON renderers to return
    deterministic strings without needing a fake DB shaped for the
    scheduler/locust SELECTs.
    """

    async def _fake_build(_db, _stack):
        return SimpleNamespace()

    def _fake_render_scheduler(_ctx):
        return scheduler_rendered

    def _fake_render_locust(_ctx):
        return locust_rendered

    monkeypatch.setattr(stacks_svc, "_build_render_context", _fake_build)
    monkeypatch.setattr(
        stacks_svc, "render_scheduler_config", _fake_render_scheduler
    )
    monkeypatch.setattr(
        stacks_svc, "render_locust_config", _fake_render_locust
    )


# ---------------------------------------------------------------------------
# 1. test_push_scheduler_writes_to_scheduler_config_json
# ---------------------------------------------------------------------------


async def test_push_scheduler_writes_to_scheduler_config_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path for the scheduler push: remote path + rendered content.

    The SFTP write must target ``<stack.stack_dir>/scheduler_config.json``
    with the bytes the renderer returned, and the audit row must use the
    Phase-5 action name ``stack.push_scheduler_config``.
    """
    server = _fake_server()
    stack = _fake_stack(server_id=server.id)

    db = MagicMock()
    db.get = AsyncMock(return_value=server)
    db.commit = AsyncMock()
    db.add = MagicMock()

    async def _fake_get_stack(_db, _sid):
        return stack

    monkeypatch.setattr(stacks_svc, "get_stack", _fake_get_stack)
    _install_lock_session(monkeypatch, acquired=True)
    _install_render_pipeline_stubs(
        monkeypatch, scheduler_rendered="<SCHED>"
    )

    sftp_atomic_write = AsyncMock()
    monkeypatch.setattr(
        stacks_svc,
        "_import_sftp",
        lambda: (sftp_atomic_write, AsyncMock()),
    )

    result = await stacks_svc.push_scheduler_config_for_stack(
        db, stack.id, actor_id=None
    )

    sftp_atomic_write.assert_awaited_once()
    args, _kwargs = sftp_atomic_write.call_args
    assert args[0] is server
    assert args[1] == f"{stack.stack_dir}/scheduler_config.json"
    assert args[2] == "<SCHED>"

    # Audit row written and transaction committed.
    db.add.assert_called_once()
    audit_row = db.add.call_args.args[0]
    assert audit_row.action == "stack.push_scheduler_config"
    db.commit.assert_awaited()

    assert result.ok is True
    assert result.stack_id == stack.id
    assert "scheduler_config.json pushed" in result.message


# ---------------------------------------------------------------------------
# 2. test_push_locust_writes_to_locust_config_json
# ---------------------------------------------------------------------------


async def test_push_locust_writes_to_locust_config_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path for the locust push: remote path + rendered content.

    The SFTP write must target ``<stack.stack_dir>/locust_config.json`` with
    the renderer's bytes, and the audit must use the Phase-5 action name
    ``stack.push_locust_config``.
    """
    server = _fake_server()
    stack = _fake_stack(server_id=server.id)

    db = MagicMock()
    db.get = AsyncMock(return_value=server)
    db.commit = AsyncMock()
    db.add = MagicMock()

    async def _fake_get_stack(_db, _sid):
        return stack

    monkeypatch.setattr(stacks_svc, "get_stack", _fake_get_stack)
    _install_lock_session(monkeypatch, acquired=True)
    _install_render_pipeline_stubs(
        monkeypatch, locust_rendered="<LOCUST>"
    )

    sftp_atomic_write = AsyncMock()
    monkeypatch.setattr(
        stacks_svc,
        "_import_sftp",
        lambda: (sftp_atomic_write, AsyncMock()),
    )

    result = await stacks_svc.push_locust_config_for_stack(
        db, stack.id, actor_id=None
    )

    sftp_atomic_write.assert_awaited_once()
    args, _kwargs = sftp_atomic_write.call_args
    assert args[0] is server
    assert args[1] == f"{stack.stack_dir}/locust_config.json"
    assert args[2] == "<LOCUST>"

    db.add.assert_called_once()
    audit_row = db.add.call_args.args[0]
    assert audit_row.action == "stack.push_locust_config"
    db.commit.assert_awaited()

    assert result.ok is True
    assert result.stack_id == stack.id
    assert "locust_config.json pushed" in result.message


# ---------------------------------------------------------------------------
# 3. test_push_scheduler_uses_per_server_lock
# ---------------------------------------------------------------------------


async def test_push_scheduler_uses_per_server_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduler push uses the same per-server lock key as config.ini.

    Proves the Phase-5 push helpers serialise against the existing
    provision/redeploy/deprovision/push_config_ini operations — all four
    take ``_compose_lock_key(server.id)``, so two of them on the same server
    take the same int key.
    """
    server = _fake_server()
    stack = _fake_stack(server_id=server.id)

    db = MagicMock()
    db.get = AsyncMock(return_value=server)
    db.commit = AsyncMock()
    db.add = MagicMock()

    async def _fake_get_stack(_db, _sid):
        return stack

    monkeypatch.setattr(stacks_svc, "get_stack", _fake_get_stack)
    state = _install_lock_session(monkeypatch, acquired=True)
    _install_render_pipeline_stubs(monkeypatch)

    monkeypatch.setattr(
        stacks_svc,
        "_import_sftp",
        lambda: (AsyncMock(), AsyncMock()),
    )

    await stacks_svc.push_scheduler_config_for_stack(
        db, stack.id, actor_id=None
    )

    # The key the scheduler push acquired must equal the key
    # push_config_ini_for_stack would have used for the same server. Compute
    # the expected key the same way the production helper does.
    expected_key = stacks_svc._compose_lock_key(server.id)
    assert state["acquire_calls"] == [expected_key]
    # Lock was released too.
    assert state["release_calls"] == [expected_key]


# ---------------------------------------------------------------------------
# 4. test_push_scheduler_raises_runtimeerror_when_lock_busy
# ---------------------------------------------------------------------------


async def test_push_scheduler_raises_runtimeerror_when_lock_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``try_acquire_session_lock`` returning False surfaces as RuntimeError.

    Mirrors the behaviour of provision/redeploy/push_config_ini: rather than
    block the request we raise so the caller can show "retry in a minute".
    Critically the SFTP layer and renderer must NOT be invoked when the
    lock is busy.
    """
    server = _fake_server()
    stack = _fake_stack(server_id=server.id)

    db = MagicMock()
    db.get = AsyncMock(return_value=server)
    db.commit = AsyncMock()

    async def _fake_get_stack(_db, _sid):
        return stack

    monkeypatch.setattr(stacks_svc, "get_stack", _fake_get_stack)
    _install_lock_session(monkeypatch, acquired=False)

    def _no_sftp():
        raise AssertionError("SFTP must not be touched when lock is busy")

    monkeypatch.setattr(stacks_svc, "_import_sftp", _no_sftp)

    async def _no_build(*_a, **_k):
        raise AssertionError("render must not run when lock is busy")

    monkeypatch.setattr(stacks_svc, "_build_render_context", _no_build)

    with pytest.raises(RuntimeError):
        await stacks_svc.push_scheduler_config_for_stack(
            db, stack.id, actor_id=None
        )


# ---------------------------------------------------------------------------
# 5. test_push_locust_raises_runtimeerror_when_lock_busy
# ---------------------------------------------------------------------------


async def test_push_locust_raises_runtimeerror_when_lock_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as test (4) but for the locust push.

    Both Phase-5 helpers share an internal body, but we pin the
    lock-busy → RuntimeError contract independently on each public entry
    point so a future refactor can't silently change one of them.
    """
    server = _fake_server()
    stack = _fake_stack(server_id=server.id)

    db = MagicMock()
    db.get = AsyncMock(return_value=server)
    db.commit = AsyncMock()

    async def _fake_get_stack(_db, _sid):
        return stack

    monkeypatch.setattr(stacks_svc, "get_stack", _fake_get_stack)
    _install_lock_session(monkeypatch, acquired=False)

    def _no_sftp():
        raise AssertionError("SFTP must not be touched when lock is busy")

    monkeypatch.setattr(stacks_svc, "_import_sftp", _no_sftp)

    async def _no_build(*_a, **_k):
        raise AssertionError("render must not run when lock is busy")

    monkeypatch.setattr(stacks_svc, "_build_render_context", _no_build)

    with pytest.raises(RuntimeError):
        await stacks_svc.push_locust_config_for_stack(
            db, stack.id, actor_id=None
        )


# ---------------------------------------------------------------------------
# 6. test_scheduler_preview_handles_missing_remote_file
# ---------------------------------------------------------------------------


async def test_scheduler_preview_handles_missing_remote_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sftp_read_text`` raising ``SSHError`` → current half is ``""``.

    On a fresh stack (or after a manual ``rm scheduler_config.json``) the
    file may simply not exist on the remote. The preview should still
    return a sensible (current="", new=<rendered>) tuple so the diff UI can
    show "this is a fresh push". No redaction is applied — the JSON has no
    secrets.
    """
    server = _fake_server()
    stack = _fake_stack(server_id=server.id)

    db = MagicMock()
    db.get = AsyncMock(return_value=server)

    async def _fake_get_stack(_db, _sid):
        return stack

    monkeypatch.setattr(stacks_svc, "get_stack", _fake_get_stack)
    _install_render_pipeline_stubs(
        monkeypatch, scheduler_rendered="<NEW SCHED>"
    )

    async def _missing_read(*_args, **_kwargs):
        raise SSHError("file not found")

    monkeypatch.setattr(
        stacks_svc,
        "_import_sftp",
        lambda: (AsyncMock(), _missing_read),
    )

    current, new = await stacks_svc.render_scheduler_config_for_stack_preview(
        db, stack.id
    )

    assert current == ""
    assert new == "<NEW SCHED>"


# ---------------------------------------------------------------------------
# 7. test_locust_preview_handles_missing_remote_file
# ---------------------------------------------------------------------------


async def test_locust_preview_handles_missing_remote_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as test (6) but for the locust preview.

    Both preview helpers share the same shape; we pin each one
    independently so a future refactor can't silently change one.
    """
    server = _fake_server()
    stack = _fake_stack(server_id=server.id)

    db = MagicMock()
    db.get = AsyncMock(return_value=server)

    async def _fake_get_stack(_db, _sid):
        return stack

    monkeypatch.setattr(stacks_svc, "get_stack", _fake_get_stack)
    _install_render_pipeline_stubs(
        monkeypatch, locust_rendered="<NEW LOCUST>"
    )

    async def _missing_read(*_args, **_kwargs):
        raise SSHError("file not found")

    monkeypatch.setattr(
        stacks_svc,
        "_import_sftp",
        lambda: (AsyncMock(), _missing_read),
    )

    current, new = await stacks_svc.render_locust_config_for_stack_preview(
        db, stack.id
    )

    assert current == ""
    assert new == "<NEW LOCUST>"
