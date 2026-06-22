"""Unit tests for the Phase-4 customer-driven push helpers.

These tests pin the behaviour of the two public coroutines we added in
Phase 4:

* :func:`app.services.stacks.push_config_ini_for_stack` — render
  ``config.ini`` from current DB state and SFTP-push under the per-server
  advisory lock. Called by the customer-mutation endpoints.
* :func:`app.services.stacks.render_config_ini_for_stack_preview` — return
  a (current_remote, new_rendered) tuple for the diff-preview UI.

No real DB, no real SSH. The SQLAlchemy session is faked with
:class:`unittest.mock.MagicMock`; ``sftp_atomic_write`` and ``sftp_read_text``
are replaced via the lazy importer hook the module exposes
(``_import_sftp``); the advisory-lock helpers are monkey-patched.
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
    state = {
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
    rendered: str = "[DEFAULT]\n[a11111111_c22222222_bbi_IRO]\n",
) -> None:
    """Skip the DB-loading half of the render pipeline.

    Tests for the pipeline itself live in ``test_render_with_customers.py``;
    here we just need ``_build_render_context`` and ``render_config_ini`` to
    return something deterministic without needing a fake DB shaped for the
    customer SELECT.
    """

    async def _fake_build(_db, _stack):
        # Return any object — render_config_ini is stubbed below.
        return SimpleNamespace()

    def _fake_render(_ctx):
        return rendered

    monkeypatch.setattr(stacks_svc, "_build_render_context", _fake_build)
    monkeypatch.setattr(stacks_svc, "render_config_ini", _fake_render)


# ---------------------------------------------------------------------------
# 1. test_push_calls_sftp_atomic_write_with_rendered_content
# ---------------------------------------------------------------------------


async def test_push_calls_sftp_atomic_write_with_rendered_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: render → sftp_atomic_write with the rendered bytes.

    The remote path must be ``<stack.stack_dir>/config.ini`` and the content
    must equal what the renderer returned. Asserts the audit log is written
    and commit is awaited.
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
    _install_render_pipeline_stubs(monkeypatch, rendered="<RENDERED>")

    sftp_atomic_write = AsyncMock()
    monkeypatch.setattr(
        stacks_svc,
        "_import_sftp",
        lambda: (sftp_atomic_write, AsyncMock()),
    )

    result = await stacks_svc.push_config_ini_for_stack(
        db, stack.id, actor_id=None
    )

    sftp_atomic_write.assert_awaited_once()
    args, _kwargs = sftp_atomic_write.call_args
    assert args[0] is server
    assert args[1] == f"{stack.stack_dir}/config.ini"
    assert args[2] == "<RENDERED>"

    # An audit row was added and the transaction committed.
    db.add.assert_called_once()
    db.commit.assert_awaited()

    # And the action result is "ok".
    assert result.ok is True
    assert result.stack_id == stack.id
    assert "config.ini pushed" in result.message


# ---------------------------------------------------------------------------
# 2. test_push_uses_per_server_advisory_lock
# ---------------------------------------------------------------------------


async def test_push_uses_per_server_advisory_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two stacks on the same server take the same advisory-lock key.

    Proves the lock keying is server-scoped (so two different stacks on the
    same box still serialise) rather than per-stack.
    """
    server = _fake_server()
    stack_a = _fake_stack(server_id=server.id)
    stack_b = _fake_stack(server_id=server.id)

    db = MagicMock()
    db.get = AsyncMock(return_value=server)
    db.commit = AsyncMock()
    db.add = MagicMock()

    # Different stack rows for the two calls.
    stack_iter = iter([stack_a, stack_b])

    async def _fake_get_stack(_db, _sid):
        return next(stack_iter)

    monkeypatch.setattr(stacks_svc, "get_stack", _fake_get_stack)

    state = _install_lock_session(monkeypatch, acquired=True)
    _install_render_pipeline_stubs(monkeypatch)

    monkeypatch.setattr(
        stacks_svc,
        "_import_sftp",
        lambda: (AsyncMock(), AsyncMock()),
    )

    await stacks_svc.push_config_ini_for_stack(
        db, stack_a.id, actor_id=None
    )
    await stacks_svc.push_config_ini_for_stack(
        db, stack_b.id, actor_id=None
    )

    assert len(state["acquire_calls"]) == 2
    # Both lock keys must equal — proving the key is per-server, not
    # per-stack.
    assert state["acquire_calls"][0] == state["acquire_calls"][1]


# ---------------------------------------------------------------------------
# 3. test_push_releases_lock_on_failure
# ---------------------------------------------------------------------------


async def test_push_releases_lock_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If sftp_atomic_write raises, the lock is still released.

    Keeps a transient SFTP failure from pinning the server-wide compose lock
    until the underlying Postgres session eventually times out.
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

    async def _exploding_write(*_args, **_kwargs):
        raise SSHError("simulated SFTP failure")

    monkeypatch.setattr(
        stacks_svc,
        "_import_sftp",
        lambda: (_exploding_write, AsyncMock()),
    )

    with pytest.raises(SSHError):
        await stacks_svc.push_config_ini_for_stack(
            db, stack.id, actor_id=None
        )

    # Acquired once, released once — the ``finally`` ran.
    assert len(state["acquire_calls"]) == 1
    assert len(state["release_calls"]) == 1
    assert state["acquire_calls"][0] == state["release_calls"][0]


# ---------------------------------------------------------------------------
# 4. test_push_raises_runtimeerror_when_lock_busy
# ---------------------------------------------------------------------------


async def test_push_raises_runtimeerror_when_lock_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``try_acquire_session_lock`` returning False surfaces as RuntimeError.

    Mirrors the behaviour of provision/redeploy: rather than blocking the
    request, we raise so the caller can show "retry in a minute". Critically
    the SFTP layer must NOT be invoked.
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

    # If the lock-busy branch fires, the SFTP and render paths must not
    # execute — wire them up as explosive guards.
    def _no_sftp():
        raise AssertionError("SFTP must not be touched when lock is busy")

    monkeypatch.setattr(stacks_svc, "_import_sftp", _no_sftp)

    async def _no_build(*_a, **_k):
        raise AssertionError("render must not run when lock is busy")

    monkeypatch.setattr(stacks_svc, "_build_render_context", _no_build)

    with pytest.raises(RuntimeError):
        await stacks_svc.push_config_ini_for_stack(
            db, stack.id, actor_id=None
        )


# ---------------------------------------------------------------------------
# 5. test_render_config_ini_for_stack_preview_handles_missing_file
# ---------------------------------------------------------------------------


async def test_render_config_ini_for_stack_preview_handles_missing_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sftp_read_text`` raising ``SSHError`` → current half is ``""``.

    On a brand-new stack or after a manual ``rm config.ini`` the file may
    simply not exist on the remote. The preview should still return a
    sensible (current="", new=<rendered>) tuple so the diff UI can show
    "this is a fresh push".
    """
    server = _fake_server()
    stack = _fake_stack(server_id=server.id)

    db = MagicMock()
    db.get = AsyncMock(return_value=server)

    async def _fake_get_stack(_db, _sid):
        return stack

    monkeypatch.setattr(stacks_svc, "get_stack", _fake_get_stack)
    _install_render_pipeline_stubs(monkeypatch, rendered="<NEW CONTENT>")

    async def _missing_read(*_args, **_kwargs):
        raise SSHError("file not found")

    monkeypatch.setattr(
        stacks_svc,
        "_import_sftp",
        lambda: (AsyncMock(), _missing_read),
    )

    current, new = await stacks_svc.render_config_ini_for_stack_preview(
        db, stack.id
    )

    assert current == ""
    assert new == "<NEW CONTENT>"


# ---------------------------------------------------------------------------
# 6. Fleet-wide push (push_config_ini_to_all_stacks)
# ---------------------------------------------------------------------------


def _fake_named_server(name: str) -> SimpleNamespace:
    s = _fake_server()
    s.name = name
    return s


async def test_fleet_push_reports_per_stack_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort fleet push: each stack's outcome is mapped to a status and
    one failure never aborts the rest. Warms the family cache + audits once."""
    srv1 = _fake_named_server("PouyanIt")
    srv2 = _fake_named_server("Tebyan")
    s1 = _fake_stack(server_id=srv1.id)   # success
    s2 = _fake_stack(server_id=srv1.id)   # host down (SSHError)
    s3 = _fake_stack(server_id=srv2.id)   # lock busy (RuntimeError)
    servers = {srv1.id: srv1, srv2.id: srv2}

    db = MagicMock()
    db.get = AsyncMock(side_effect=lambda _model, sid: servers.get(sid))
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def _fake_list(_db):
        return [s1, s2, s3]

    monkeypatch.setattr(stacks_svc, "list_stacks", _fake_list)
    monkeypatch.setattr(
        "app.services.brokers.registry.warm_family_cache", AsyncMock()
    )
    _install_lock_session(monkeypatch)  # patches AsyncSessionLocal

    outcomes = {s1.id: None, s2.id: SSHError("down"), s3.id: RuntimeError("busy")}

    async def _fake_push(_push_db, sid, _actor):
        exc = outcomes[sid]
        if exc is not None:
            raise exc

    monkeypatch.setattr(stacks_svc, "push_config_ini_for_stack", _fake_push)

    res = await stacks_svc.push_config_ini_to_all_stacks(db, actor_id=None)

    assert (res.total, res.succeeded, res.failed) == (3, 1, 2)
    by_stack = {i.stack_id: i for i in res.items}
    assert by_stack[s1.id].ok and by_stack[s1.id].status == "pushed"
    assert by_stack[s2.id].status == "host_down" and not by_stack[s2.id].ok
    assert by_stack[s3.id].status == "lock_busy"
    assert by_stack[s1.id].server_name == "PouyanIt"
    # one fleet-level audit row + commit
    db.add.assert_called_once()
    db.commit.assert_awaited()


async def test_fleet_push_only_stack_ids_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``only_stack_ids`` (retry-failed) pushes just the requested stacks."""
    srv = _fake_named_server("PouyanIt")
    s1 = _fake_stack(server_id=srv.id)
    s2 = _fake_stack(server_id=srv.id)

    db = MagicMock()
    db.get = AsyncMock(return_value=srv)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def _fake_list(_db):
        return [s1, s2]

    pushed: list = []

    async def _fake_push(_push_db, sid, _actor):
        pushed.append(sid)

    monkeypatch.setattr(stacks_svc, "list_stacks", _fake_list)
    monkeypatch.setattr(
        "app.services.brokers.registry.warm_family_cache", AsyncMock()
    )
    _install_lock_session(monkeypatch)
    monkeypatch.setattr(stacks_svc, "push_config_ini_for_stack", _fake_push)

    res = await stacks_svc.push_config_ini_to_all_stacks(
        db, actor_id=None, only_stack_ids=[s2.id]
    )

    assert pushed == [s2.id]
    assert res.total == 1 and res.succeeded == 1
