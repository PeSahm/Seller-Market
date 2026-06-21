"""Tests for the mgmt-instance heartbeat (#156 HA visibility)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import instance_heartbeat as ih


class _Row:
    def __init__(self, name, ago_s, *, is_leader=False, active_db="main"):
        self.name = name
        self.address = None
        self.version = None
        self.active_db = active_db
        self.is_leader = is_leader
        self.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=ago_s)


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _DB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *a, **k):
        return _Result(self._rows)


@pytest.mark.asyncio
async def test_list_instances_flags_stale():
    db = _DB([_Row("ParsPack", 120), _Row("PouyanIt", 5, is_leader=True)])
    out = await ih.list_instances(db, stale_after_seconds=60)
    assert {i["name"] for i in out} == {"PouyanIt", "ParsPack"}
    pouyan = next(i for i in out if i["name"] == "PouyanIt")
    assert pouyan["is_leader"] is True and pouyan["stale"] is False
    pars = next(i for i in out if i["name"] == "ParsPack")
    assert pars["is_leader"] is False and pars["stale"] is True  # 120s > 60s


@pytest.mark.asyncio
async def test_heartbeat_worker_upserts_and_survives_errors(monkeypatch):
    app = SimpleNamespace(state=SimpleNamespace(is_worker_leader=True))
    seen = []

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(ih, "AsyncSessionLocal", lambda: _Sess())

    calls = {"n": 0}

    async def _upsert(db, **kw):
        calls["n"] += 1
        seen.append((kw["name"], kw["is_leader"], kw["active_db"]))
        if calls["n"] == 1:
            raise RuntimeError("transient")  # must NOT kill the loop

    monkeypatch.setattr(ih, "upsert_instance", _upsert)
    monkeypatch.setattr(ih.db_mod, "active_db", lambda: "main")
    monkeypatch.setattr(
        ih, "get_settings",
        lambda: SimpleNamespace(
            instance_heartbeat_interval_seconds=0.01,
            mgmt_instance_address="",
            app_version="x",
            resolved_instance_name=lambda: "PouyanIt",
        ),
    )

    stop = asyncio.Event()
    task = asyncio.create_task(ih.run_heartbeat_worker(app, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task

    assert calls["n"] >= 2  # survived the first error and kept beating
    assert seen[0] == ("PouyanIt", True, "main")
