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

import gzip
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.scheduled_run_ingestor import (
    _archive_log_if_final,
    _parse_iso,
    _upsert_run_from_marker,
)
from app.services.ssh.exceptions import SSHError


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
    action, _, _ = await _upsert_run_from_marker(
        _FakeAsyncSession(),
        stack=_fake_stack(),
        payload={"schema_version": 99, "job_name": "cache_warmup"},
    )
    assert action == "skipped"


@pytest.mark.asyncio
async def test_upsert_skips_unknown_job_name() -> None:
    """job_name not in the runs enum → skip (fail closed on drift)."""
    action, reason, _ = await _upsert_run_from_marker(
        _FakeAsyncSession(),
        stack=_fake_stack(),
        payload={"schema_version": 1, "job_name": "exotic", "scheduled_run_id": str(uuid.uuid4())},
    )
    assert action == "skipped" and "enum" in reason


@pytest.mark.asyncio
async def test_upsert_skips_malformed_uuid() -> None:
    """Garbage scheduled_run_id → skip (a bad UUID can't be a PK)."""
    action, _, _ = await _upsert_run_from_marker(
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
    action, reason, _ = await _upsert_run_from_marker(
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
        action, _, _ = await _upsert_run_from_marker(
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
        action, _, _ = await _upsert_run_from_marker(
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
        action, _, _ = await _upsert_run_from_marker(
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


# ---------------------------------------------------------------------------
# _archive_log_if_final — full gzipped log fetch (with tails fallback)
# ---------------------------------------------------------------------------

def _fake_run_row() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), log_blob_ref=None, log_blob_sha256=None)


def _fake_server() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), host="203.0.113.7")


def _stack_with_dir() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), agent_id=uuid.uuid4(),
        stack_dir="/root/seller-market/agents/abc",
    )


def _final_payload(rid: str, *, log_file: str | None = None) -> dict:
    payload = {
        "schema_version": 1, "job_name": "run_trading",
        "scheduled_run_id": rid, "status": "failed", "exit_code": 1,
        "stdout_tail": "tail-out\n", "stderr_tail": "tail-err\n",
    }
    if log_file is not None:
        payload["log_file"] = log_file
    return payload


def _patch_settings(tmp_path):
    return patch(
        "app.services.scheduled_run_ingestor.get_settings",
        return_value=SimpleNamespace(run_logs_dir=str(tmp_path)),
    )


@pytest.mark.asyncio
async def test_archive_fetches_full_gz_and_stores_verbatim(tmp_path) -> None:
    """Happy path: gz fetched, verified, stored AS-FETCHED; remote path returned."""
    run = _fake_run_row()
    rid = str(uuid.uuid4())
    gz_bytes = gzip.compress(b"the FULL run output\n" * 1000)
    with _patch_settings(tmp_path), patch(
        "app.services.ssh.sftp.sftp_read_bytes",
        new_callable=AsyncMock, return_value=gz_bytes,
    ):
        consumed = await _archive_log_if_final(
            run, _final_payload(rid, log_file=f"scheduled_run_{rid}.log.gz"),
            server=_fake_server(), stack=_stack_with_dir(),
        )
    assert consumed == (
        f"/root/seller-market/agents/abc/run_results/scheduled_run_{rid}.log.gz"
    )
    assert run.log_blob_ref.endswith(f"{run.id}.log.gz")
    stored = Path(run.log_blob_ref).read_bytes()
    assert stored == gz_bytes                      # verbatim, no re-compression
    assert run.log_blob_sha256 == hashlib.sha256(gz_bytes).hexdigest()
    assert gzip.decompress(stored) == b"the FULL run output\n" * 1000


@pytest.mark.asyncio
async def test_archive_rejects_foreign_log_file_name(tmp_path) -> None:
    """log_file must be EXACTLY scheduled_run_<id>.log.gz — traversal attempts
    and any other name fall back to the marker tails."""
    rid = str(uuid.uuid4())
    for bad_name in ("../../etc/shadow", "scheduled_run_other.log.gz", "x.log.gz"):
        run = _fake_run_row()
        with _patch_settings(tmp_path), patch(
            "app.services.ssh.sftp.sftp_read_bytes", new_callable=AsyncMock,
        ) as fetch:
            consumed = await _archive_log_if_final(
                run, _final_payload(rid, log_file=bad_name),
                server=_fake_server(), stack=_stack_with_dir(),
            )
        fetch.assert_not_awaited()
        assert consumed is None
        assert run.log_blob_ref.endswith(f"{run.id}.log")  # tails fallback
        assert b"tail-out" in Path(run.log_blob_ref).read_bytes()


@pytest.mark.asyncio
async def test_archive_falls_back_on_fetch_error_and_corrupt_gz(tmp_path) -> None:
    rid = str(uuid.uuid4())
    name = f"scheduled_run_{rid}.log.gz"

    # SSH failure → tails, no consumed path (remote gz left for retry).
    run = _fake_run_row()
    with _patch_settings(tmp_path), patch(
        "app.services.ssh.sftp.sftp_read_bytes",
        new_callable=AsyncMock, side_effect=SSHError("boom"),
    ):
        consumed = await _archive_log_if_final(
            run, _final_payload(rid, log_file=name),
            server=_fake_server(), stack=_stack_with_dir(),
        )
    assert consumed is None and run.log_blob_ref.endswith(f"{run.id}.log")

    # Corrupt gz → tails.
    run = _fake_run_row()
    with _patch_settings(tmp_path), patch(
        "app.services.ssh.sftp.sftp_read_bytes",
        new_callable=AsyncMock, return_value=b"not gzip",
    ):
        consumed = await _archive_log_if_final(
            run, _final_payload(rid, log_file=name),
            server=_fake_server(), stack=_stack_with_dir(),
        )
    assert consumed is None and run.log_blob_ref.endswith(f"{run.id}.log")


@pytest.mark.asyncio
async def test_archive_rejects_oversized_decompression(tmp_path, monkeypatch) -> None:
    """Gzip-bomb guard: payload decompressing past the cap → tails fallback."""
    import app.services.scheduled_run_ingestor as ing

    monkeypatch.setattr(ing, "_MAX_LOG_BYTES", 16)
    rid = str(uuid.uuid4())
    run = _fake_run_row()
    with _patch_settings(tmp_path), patch(
        "app.services.ssh.sftp.sftp_read_bytes",
        new_callable=AsyncMock, return_value=gzip.compress(b"X" * 1000),
    ):
        consumed = await _archive_log_if_final(
            run, _final_payload(rid, log_file=f"scheduled_run_{rid}.log.gz"),
            server=_fake_server(), stack=_stack_with_dir(),
        )
    assert consumed is None
    assert run.log_blob_ref.endswith(f"{run.id}.log")


@pytest.mark.asyncio
async def test_archive_without_log_file_keeps_tails_behavior(tmp_path) -> None:
    """Old bot images (no log_file field) keep the exact pre-existing path."""
    run = _fake_run_row()
    rid = str(uuid.uuid4())
    with _patch_settings(tmp_path):
        consumed = await _archive_log_if_final(
            run, _final_payload(rid),
            server=_fake_server(), stack=_stack_with_dir(),
        )
    assert consumed is None
    assert run.log_blob_ref.endswith(f"{run.id}.log")
    blob = Path(run.log_blob_ref).read_bytes()
    assert blob == b"tail-out\n" + b"\n--- stderr ---\n" + b"tail-err\n"


# ---------------------------------------------------------------------------
# Bounded full-log retry (terminal row + surviving marker)
# ---------------------------------------------------------------------------


def _terminal_row(*, blob_ref: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), status="failed", exit_code=1,
        finished_at=datetime.now(timezone.utc),
        log_blob_ref=blob_ref, log_blob_sha256=None,
    )


def _retry_payload(rid: str, *, finished_at: str) -> dict:
    return {
        "schema_version": 1, "job_name": "run_trading",
        "scheduled_run_id": rid, "status": "failed", "exit_code": 1,
        "finished_at": finished_at,
        "log_file": f"scheduled_run_{rid}.log.gz",
        "stdout_tail": "t\n", "stderr_tail": "",
    }


@pytest.mark.asyncio
async def test_terminal_row_full_log_retry_success() -> None:
    """Marker survived a tails fallback; within the window a later tick
    re-fetches the gz and the caller gets the consumed path to delete."""
    rid = uuid.uuid4()
    row = _terminal_row(blob_ref="/var/lib/run_logs/x.log")  # tails, not .gz
    fresh = datetime.now(timezone.utc).isoformat()
    with patch(
        "app.services.scheduled_run_ingestor._archive_log_if_final",
        new_callable=AsyncMock, return_value="/remote/run_results/got.log.gz",
    ) as archive:
        action, reason, consumed = await _upsert_run_from_marker(
            _FakeAsyncSession(existing_run=row), stack=_fake_stack(),
            payload=_retry_payload(str(rid), finished_at=fresh),
            server=_fake_server(),
        )
    archive.assert_awaited_once()
    assert action == "updated" and consumed == "/remote/run_results/got.log.gz"


@pytest.mark.asyncio
async def test_terminal_row_full_log_retry_pending_keeps_marker() -> None:
    """Fetch fails again within the window → skipped (marker survives)."""
    rid = uuid.uuid4()
    row = _terminal_row(blob_ref="/var/lib/run_logs/x.log")
    fresh = datetime.now(timezone.utc).isoformat()
    with patch(
        "app.services.scheduled_run_ingestor._archive_log_if_final",
        new_callable=AsyncMock, return_value=None,
    ):
        action, reason, consumed = await _upsert_run_from_marker(
            _FakeAsyncSession(existing_run=row), stack=_fake_stack(),
            payload=_retry_payload(str(rid), finished_at=fresh),
            server=_fake_server(),
        )
    assert action == "skipped" and "retry pending" in reason and consumed is None


@pytest.mark.asyncio
async def test_terminal_row_full_log_retry_window_expired() -> None:
    """>24h after finished_at → give up: marker consumed, tails stand."""
    from datetime import timedelta

    rid = uuid.uuid4()
    row = _terminal_row(blob_ref="/var/lib/run_logs/x.log")
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    with patch(
        "app.services.scheduled_run_ingestor._archive_log_if_final",
        new_callable=AsyncMock,
    ) as archive:
        action, reason, consumed = await _upsert_run_from_marker(
            _FakeAsyncSession(existing_run=row), stack=_fake_stack(),
            payload=_retry_payload(str(rid), finished_at=stale),
            server=_fake_server(),
        )
    archive.assert_not_awaited()
    assert action == "updated" and "expired" in reason and consumed is None


@pytest.mark.asyncio
async def test_terminal_row_with_gz_blob_already_archived_skips() -> None:
    """Row already carries the full gz → plain terminal-collision skip."""
    rid = uuid.uuid4()
    row = _terminal_row(blob_ref="/var/lib/run_logs/x.log.gz")
    action, reason, consumed = await _upsert_run_from_marker(
        _FakeAsyncSession(existing_run=row), stack=_fake_stack(),
        payload=_retry_payload(str(rid), finished_at=datetime.now(timezone.utc).isoformat()),
        server=_fake_server(),
    )
    assert action == "skipped" and "terminal" in reason and consumed is None
