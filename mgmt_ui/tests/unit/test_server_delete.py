"""Unit tests for soft_delete_server (the delete-server button).

Four tables FK servers.id with ON DELETE RESTRICT. The service must:
  * REFUSE (ServerInUseError, not a DB-500) when customers or stacks remain,
  * clear the blocking telemetry/policy refs and delete otherwise,
  * no-op on a missing server.
get_server / _public_snapshot / _write_audit / _delete_key_file are stubbed so
only the delete logic is exercised; a fake session counts the rest.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import servers as svc


class _CountRes:
    def __init__(self, n):
        self._n = n

    def scalar(self):
        return self._n


class _Dummy:
    pass


class _FakeDB:
    def __init__(self, results):
        self._r = list(results)  # results for the count queries, in order
        self.deleted: list = []
        self.added: list = []
        self.commits = 0
        self.executes = 0

    async def execute(self, stmt):
        self.executes += 1
        return self._r.pop(0) if self._r else _Dummy()

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1


@pytest.fixture
def patched(monkeypatch):
    srv = SimpleNamespace(id=uuid.uuid4(), ssh_auth="password")

    async def _get(_db, _sid):
        return srv
    monkeypatch.setattr(svc, "get_server", _get)
    monkeypatch.setattr(svc, "_public_snapshot", lambda s: {})

    async def _audit(*a, **k):
        return None
    monkeypatch.setattr(svc, "_write_audit", _audit)
    monkeypatch.setattr(svc, "_delete_key_file", lambda sid: None)
    return srv


async def test_delete_blocked_when_customers_or_stacks(patched):
    db = _FakeDB([_CountRes(2), _CountRes(0)])  # 2 customers, 0 stacks
    with pytest.raises(svc.ServerInUseError):
        await svc.soft_delete_server(db, patched.id, actor_id=uuid.uuid4())
    assert db.commits == 0      # nothing committed
    assert db.deleted == []     # server row NOT deleted


async def test_delete_blocked_counts_stacks_too(patched):
    db = _FakeDB([_CountRes(0), _CountRes(3)])  # 0 customers, 3 stacks
    with pytest.raises(svc.ServerInUseError):
        await svc.soft_delete_server(db, patched.id, actor_id=uuid.uuid4())
    assert db.commits == 0


async def test_delete_clears_refs_and_deletes(patched):
    # No customers/stacks → clears clock-skew samples + policy ptr, deletes row.
    db = _FakeDB([_CountRes(0), _CountRes(0)])
    await svc.soft_delete_server(db, patched.id, actor_id=uuid.uuid4())
    assert patched in db.deleted          # the server row was deleted
    assert db.commits == 1
    # 2 count queries + the cleanup DELETE + the policy UPDATE = 4 executes.
    assert db.executes == 4


async def test_delete_missing_server_is_noop(monkeypatch):
    async def _get(_db, _sid):
        return None
    monkeypatch.setattr(svc, "get_server", _get)
    db = _FakeDB([])
    await svc.soft_delete_server(db, uuid.uuid4(), actor_id=uuid.uuid4())
    assert db.commits == 0
    assert db.deleted == []
