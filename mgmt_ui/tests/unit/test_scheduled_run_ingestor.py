"""Unit tests for the scheduled-run-marker ingestor (issue #62).

We pin the pure / cheap parts:

* :func:`_parse_iso` — silently returns ``None`` on garbage so a
  malformed marker can't crash the worker tick.
* :func:`_upsert_run_from_marker` — the dispatch logic between INSERT
  (first sight) and UPDATE (running → terminal), plus the skip
  branches (wrong schema, unknown job_name, malformed UUID, terminal
  row hit again). The full DB I/O lives behind a tiny ``AsyncSession``
  fake — same pattern as ``test_force_kill_run_*``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.scheduled_run_ingestor import (
    _parse_iso,
    _upsert_run_from_marker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_stack() -> SimpleNamespace:
    """Minimal ``AgentStack`` stand-in — we only read ``id`` and ``agent_id``."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
    )


class _FakeAsyncSession:
    """``AsyncSession``-shaped mock — captures ``add`` + ``get`` calls."""

    def __init__(self, existing_run=None) -> None:
        self._run = existing_run
        self.added: list = []

    async def get(self, model, key):
        return self._run

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        return None


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------


def test_parse_iso_accepts_z_suffix() -> None:
    """A ``Z`` suffix is the canonical UTC marker; convert to +00:00."""
    dt = _parse_iso("2026-05-19T10:30:00Z")
    assert dt is not None and dt.tzinfo is timezone.utc


def test_parse_iso_returns_none_on_garbage() -> None:
    """Malformed strings can never crash the worker."""
    assert _parse_iso(None) is None
    assert _parse_iso("") is None
    assert _parse_iso("not-a-date") is None


def test_parse_iso_assumes_utc_when_naive() -> None:
    """A naive ISO string gets ``tzinfo=UTC`` so DB writes have a tz."""
    dt = _parse_iso("2026-05-19T10:30:00")
    assert dt is not None and dt.tzinfo is timezone.utc


# ---------------------------------------------------------------------------
# _upsert_run_from_marker — skip branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_skips_unsupported_schema() -> None:
    """Bot at a future schema version → skip, don't crash."""
    action, _ = await _upsert_run_from_marker(
        _FakeAsyncSession(),
        stack=_fake_stack(),
        payload={"schema_version": 99, "job_name": "cache_warmup"},
    )
    assert action == "skipped"


@pytest.mark.asyncio
async def test_upsert_skips_unknown_job_name() -> None:
    """job_name not in the runs enum → skip (fail closed on drift)."""
    action, reason = await _upsert_run_from_marker(
        _FakeAsyncSession(),
        stack=_fake_stack(),
        payload={"schema_version": 1, "job_name": "exotic", "scheduled_run_id": str(uuid.uuid4())},
    )
    assert action == "skipped" and "enum" in reason


@pytest.mark.asyncio
async def test_upsert_skips_malformed_uuid() -> None:
    """Garbage scheduled_run_id → skip (a bad UUID can't be a PK)."""
    action, _ = await _upsert_run_from_marker(
        _FakeAsyncSession(),
        stack=_fake_stack(),
        payload={"schema_version": 1, "job_name": "cache_warmup", "scheduled_run_id": "not-a-uuid"},
    )
    assert action == "skipped"


@pytest.mark.asyncio
async def test_upsert_skips_terminal_row_with_new_marker() -> None:
    """A second final marker for an already-terminal row is a no-op.

    Protects against the bot re-emitting a final marker (e.g. the
    delete-after-ingest racing with another tick).
    """
    rid = uuid.uuid4()
    terminal = SimpleNamespace(
        id=rid, status="success", exit_code=0, finished_at=datetime.now(timezone.utc),
    )
    action, reason = await _upsert_run_from_marker(
        _FakeAsyncSession(existing_run=terminal),
        stack=_fake_stack(),
        payload={
            "schema_version": 1, "job_name": "cache_warmup",
            "scheduled_run_id": str(rid), "status": "failed",
        },
    )
    assert action == "skipped" and "terminal" in reason


# ---------------------------------------------------------------------------
# _upsert_run_from_marker — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_inserts_running_marker_on_first_sight() -> None:
    """First time we see a scheduled_run_id, INSERT a status=running row."""
    db = _FakeAsyncSession(existing_run=None)
    rid = uuid.uuid4()
    stack = _fake_stack()
    with patch(
        "app.services.scheduled_run_ingestor._archive_log_if_final",
        new_callable=AsyncMock,
    ):
        action, _ = await _upsert_run_from_marker(
            db, stack=stack,
            payload={
                "schema_version": 1, "job_name": "cache_warmup",
                "scheduled_run_id": str(rid),
                "trigger": "scheduled", "status": "running",
                "started_at": "2026-05-19T10:00:00Z",
            },
        )
    assert action == "inserted"
    # One Run row + one AuditLog row should have been added.
    assert len(db.added) == 2
    # The Run object the service constructed should carry our id.
    run_obj = next(o for o in db.added if hasattr(o, "stack_id"))
    assert run_obj.id == rid
    assert run_obj.status == "running"
    assert run_obj.stack_id == stack.id


@pytest.mark.asyncio
async def test_upsert_inserts_final_marker_skipping_running_phase() -> None:
    """A final marker that's the first time we see the id → INSERT directly
    with terminal status. Happens when one tick lands the start+final
    files together (slow worker, busy bot)."""
    db = _FakeAsyncSession(existing_run=None)
    rid = uuid.uuid4()
    with patch(
        "app.services.scheduled_run_ingestor._archive_log_if_final",
        new_callable=AsyncMock,
    ) as archive_mock:
        action, _ = await _upsert_run_from_marker(
            db, stack=_fake_stack(),
            payload={
                "schema_version": 1, "job_name": "run_trading",
                "scheduled_run_id": str(rid),
                "trigger": "scheduled", "status": "success", "exit_code": 0,
                "started_at": "2026-05-19T10:00:00Z",
                "finished_at": "2026-05-19T10:02:30Z",
                "stdout_tail": "ok\n", "stderr_tail": "",
            },
        )
    assert action == "inserted"
    # The archive helper should run on final markers.
    archive_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_updates_running_row_to_terminal() -> None:
    """Existing status=running row + final marker → UPDATE in place.

    This is the common transition: tick N inserts running, tick N+1 sees
    the final marker and flips status / exit_code / finished_at.
    """
    rid = uuid.uuid4()
    running = SimpleNamespace(
        id=rid, status="running", exit_code=None, finished_at=None,
    )
    db = _FakeAsyncSession(existing_run=running)
    with patch(
        "app.services.scheduled_run_ingestor._archive_log_if_final",
        new_callable=AsyncMock,
    ):
        action, _ = await _upsert_run_from_marker(
            db, stack=_fake_stack(),
            payload={
                "schema_version": 1, "job_name": "cache_warmup",
                "scheduled_run_id": str(rid),
                "trigger": "scheduled", "status": "failed", "exit_code": 2,
                "started_at": "2026-05-19T10:00:00Z",
                "finished_at": "2026-05-19T10:05:00Z",
                "stdout_tail": "", "stderr_tail": "boom\n",
            },
        )
    assert action == "updated"
    assert running.status == "failed"
    assert running.exit_code == 2
    assert running.finished_at is not None
