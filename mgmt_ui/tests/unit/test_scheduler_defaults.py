"""Unit tests for default scheduler-job seeding on new stacks.

``ensure_default_scheduler_jobs`` creates the two canonical jobs
(cache_warmup 08:30:00, run_trading 08:44:20) for a stack that is MISSING them,
and leaves any existing job untouched (only-if-missing / idempotent). Mocks
get_job/upsert_job so no DB is needed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import scheduler_jobs as sj


async def test_creates_both_when_missing(monkeypatch):
    monkeypatch.setattr(sj, "get_job", AsyncMock(return_value=None))
    calls = []

    async def fake_upsert(db, stack_id, name, data, actor_id):
        calls.append((name, data.time, data.command, data.enabled))

    monkeypatch.setattr(sj, "upsert_job", fake_upsert)
    db = AsyncMock()
    created = await sj.ensure_default_scheduler_jobs(db, uuid4(), uuid4())

    assert created == ["cache_warmup", "run_trading"]
    assert ("cache_warmup", "08:30:00", None, True) in calls
    assert ("run_trading", "08:44:20", None, True) in calls
    db.commit.assert_awaited()


async def test_skips_existing_job(monkeypatch):
    async def fake_get(db, stack_id, name):
        return object() if name == "cache_warmup" else None

    monkeypatch.setattr(sj, "get_job", fake_get)
    calls = []

    async def fake_upsert(db, stack_id, name, data, actor_id):
        calls.append(name)

    monkeypatch.setattr(sj, "upsert_job", fake_upsert)
    db = AsyncMock()
    created = await sj.ensure_default_scheduler_jobs(db, uuid4(), uuid4())

    assert created == ["run_trading"]   # cache_warmup left untouched
    assert calls == ["run_trading"]


async def test_noop_when_both_exist(monkeypatch):
    monkeypatch.setattr(sj, "get_job", AsyncMock(return_value=object()))

    async def fake_upsert(*a, **k):
        raise AssertionError("must not upsert when both jobs already exist")

    monkeypatch.setattr(sj, "upsert_job", fake_upsert)
    db = AsyncMock()
    created = await sj.ensure_default_scheduler_jobs(db, uuid4(), uuid4())

    assert created == []
    db.commit.assert_not_awaited()


def test_default_times_are_canonical():
    from app.schemas.scheduler import DEFAULT_JOB_TIMES
    assert DEFAULT_JOB_TIMES == {"cache_warmup": "08:30:00", "run_trading": "08:44:20"}
