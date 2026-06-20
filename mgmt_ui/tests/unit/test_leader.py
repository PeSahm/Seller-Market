"""Tests for worker leader election (#156 WS3).

The DB/advisory-lock layer is mocked so the election logic (acquire / hold /
fail-open) is exercised without a live database.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import leader


def _app():
    return SimpleNamespace(state=SimpleNamespace())


@pytest.mark.asyncio
async def test_acquire_when_lock_is_free_holds_the_session(monkeypatch):
    closed = []

    class FakeSession:
        async def close(self):
            closed.append(1)

    sess = FakeSession()
    monkeypatch.setattr(leader, "AsyncSessionLocal", lambda: sess)
    monkeypatch.setattr(leader, "try_acquire_session_lock", AsyncMock(return_value=True))

    app = _app()
    assert await leader.acquire_worker_leadership(app) is True
    # the session is HELD (not closed) so the advisory lock stays held
    assert app.state._leader_session is sess
    assert closed == []


@pytest.mark.asyncio
async def test_not_acquired_when_lock_taken_closes_session(monkeypatch):
    closed = []

    class FakeSession:
        async def close(self):
            closed.append(1)

    sess = FakeSession()
    monkeypatch.setattr(leader, "AsyncSessionLocal", lambda: sess)
    monkeypatch.setattr(leader, "try_acquire_session_lock", AsyncMock(return_value=False))

    app = _app()
    assert await leader.acquire_worker_leadership(app) is False
    assert app.state._leader_session is None
    assert closed == [1]


@pytest.mark.asyncio
async def test_fail_open_on_error_assumes_leadership(monkeypatch):
    """A transient DB/lock error must NOT silently kill a single instance's
    workers — fail open (assume leadership)."""
    closed = []

    class FakeSession:
        async def close(self):
            closed.append(1)

    sess = FakeSession()
    monkeypatch.setattr(leader, "AsyncSessionLocal", lambda: sess)
    monkeypatch.setattr(
        leader, "try_acquire_session_lock", AsyncMock(side_effect=RuntimeError("db down"))
    )

    app = _app()
    assert await leader.acquire_worker_leadership(app) is True
    assert app.state._leader_session is None
    assert closed == [1]


@pytest.mark.asyncio
async def test_release_closes_held_session(monkeypatch):
    closed = []

    class FakeSession:
        async def close(self):
            closed.append(1)

    app = _app()
    app.state._leader_session = FakeSession()
    await leader.release_worker_leadership(app)
    assert app.state._leader_session is None
    assert closed == [1]

    # idempotent when nothing held
    await leader.release_worker_leadership(app)
