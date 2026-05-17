"""Filter / DELETE logic tests for the janitor.

The DB and SSH layers are mocked entirely -- these tests assert pure
filtering behaviour:

* :func:`cleanup_old_health_signals` issues a ``DELETE WHERE ts < cutoff
  AND ack_at IS NOT NULL`` (un-acked rows are exempt).
* :func:`cleanup_run_logs` deletes only ``<uuid>.log`` files older than
  the retention horizon, ignores non-matching filenames, and issues
  ONE bulk ``UPDATE runs SET log_blob_ref=NULL, log_blob_sha256=NULL``
  for the deleted run-ids.
* :func:`cleanup_ingested_order_results` only deletes files whose
  filename is ``<= cursor.last_filename`` AND whose mtime is older than
  the retention horizon. The remote ``rm`` command is shell-quoted and
  composes ``<stack_dir>/order_results/<filename>`` exactly.

Note: ``run_command`` is lazy-imported inside
:func:`cleanup_ingested_order_results`, so we patch it at the SOURCE
module ``app.services.ssh.commands`` (same pattern as the trade
ingestor tests).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRemoteResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class _FakeDB:
    """Minimal AsyncSession stand-in.

    * ``get(model, key)`` returns whatever was set in ``_lookup``.
    * ``execute(stmt)`` records the statement and returns a result whose
      ``rowcount`` is taken from ``_rowcount`` (default 0). ``scalars()``
      / ``scalar()`` paths are wired for the ``select(AgentStack)``
      branch.
    """

    def __init__(self) -> None:
        self._lookup: dict[tuple[type, object], object] = {}
        self._select_returns: list = []
        self.statements: list = []
        self._rowcount = 0
        self.commits = 0
        self.rollbacks = 0

    def set_lookup(self, model, key, obj):
        self._lookup[(model, key)] = obj

    def set_rowcount(self, n: int) -> None:
        self._rowcount = n

    def set_select_returns(self, rows: list) -> None:
        self._select_returns = rows

    async def get(self, model, key):
        return self._lookup.get((model, key))

    async def execute(self, stmt):
        self.statements.append(stmt)
        result = MagicMock()
        result.rowcount = self._rowcount
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=list(self._select_returns))
        result.scalars = MagicMock(return_value=scalars)
        return result

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


# ---------------------------------------------------------------------------
# cleanup_old_health_signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_signals_delete_uses_correct_filters():
    """Verify the DELETE statement filters on ts<cutoff AND ack_at IS NOT NULL."""
    from app.services import janitor as svc
    from app.models.health import HealthSignal

    db = _FakeDB()
    db.set_rowcount(7)

    before = datetime.now(timezone.utc)
    result = await svc.cleanup_old_health_signals(db, retention_days=30)
    after = datetime.now(timezone.utc)

    assert result.rows_deleted == 7
    assert result.errors == []
    assert db.commits == 1
    assert len(db.statements) == 1

    stmt = db.statements[0]
    # The statement must compile to SQL containing the un-acked exemption.
    sql = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "health_signals" in sql.lower()
    assert "delete" in sql.lower()
    assert "ack_at is not null" in sql.lower()
    assert "ts" in sql.lower()

    # The cutoff value bound into the statement should be ~30 days ago.
    binds = list(stmt.compile().params.values())
    cutoff_values = [v for v in binds if isinstance(v, datetime)]
    assert cutoff_values, "expected a datetime cutoff bound into the DELETE"
    cutoff = cutoff_values[0]
    expected_lower = before - timedelta(days=30, seconds=2)
    expected_upper = after - timedelta(days=30) + timedelta(seconds=2)
    assert expected_lower <= cutoff <= expected_upper, (
        f"cutoff {cutoff!r} not within 30d window "
        f"[{expected_lower!r}, {expected_upper!r}]"
    )


@pytest.mark.asyncio
async def test_health_signals_delete_handles_zero_rows():
    from app.services import janitor as svc

    db = _FakeDB()
    db.set_rowcount(0)
    result = await svc.cleanup_old_health_signals(db, retention_days=1)
    assert result.rows_deleted == 0
    assert result.errors == []
    assert db.commits == 1


@pytest.mark.asyncio
async def test_health_signals_delete_handles_db_error():
    from app.services import janitor as svc

    class _BrokenDB(_FakeDB):
        async def execute(self, stmt):
            raise RuntimeError("boom")

    db = _BrokenDB()
    result = await svc.cleanup_old_health_signals(db, retention_days=1)
    assert result.rows_deleted == 0
    assert result.errors and "boom" in result.errors[0]
    assert db.rollbacks == 1


# ---------------------------------------------------------------------------
# cleanup_run_logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_logs_deletes_old_only_and_nulls_refs(tmp_path: Path):
    from app.services import janitor as svc

    uuid_old = "11111111-1111-1111-1111-111111111111"
    uuid_new = "22222222-2222-2222-2222-222222222222"

    old_file = tmp_path / f"{uuid_old}.log"
    new_file = tmp_path / f"{uuid_new}.log"
    foreign = tmp_path / "random.txt"
    old_file.write_text("old contents")
    new_file.write_text("new contents")
    foreign.write_text("not a log")

    # Old file: 30 days ago.  New file: 1 hour ago.
    now = time.time()
    old_epoch = now - 30 * 86400
    new_epoch = now - 3600
    os.utime(old_file, (old_epoch, old_epoch))
    os.utime(new_file, (new_epoch, new_epoch))
    os.utime(foreign, (old_epoch, old_epoch))  # foreign is OLD too -- must still be ignored

    db = _FakeDB()
    db.set_rowcount(1)

    result = await svc.cleanup_run_logs(
        db, retention_days=7, run_logs_dir=tmp_path
    )

    assert result.files_scanned == 2, "only the two <uuid>.log files count"
    assert result.files_deleted == 1, "only the old uuid file is deleted"
    assert result.errors == []
    assert not old_file.exists()
    assert new_file.exists()
    assert foreign.exists(), "foreign files must not be touched"

    # One bulk UPDATE issued.
    assert len(db.statements) == 1
    stmt = db.statements[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "update" in sql.lower()
    assert "runs" in sql.lower()
    assert "log_blob_ref" in sql.lower()
    assert "log_blob_sha256" in sql.lower()

    # rows_nulled comes from the mocked rowcount.
    assert result.rows_nulled == 1
    assert db.commits == 1


@pytest.mark.asyncio
async def test_run_logs_no_directory_returns_clean(tmp_path: Path):
    from app.services import janitor as svc

    missing = tmp_path / "does-not-exist"
    db = _FakeDB()
    result = await svc.cleanup_run_logs(
        db, retention_days=7, run_logs_dir=missing
    )
    assert result.files_scanned == 0
    assert result.files_deleted == 0
    assert result.rows_nulled == 0
    assert result.errors == []
    # No DB statement should have been issued.
    assert db.statements == []


@pytest.mark.asyncio
async def test_run_logs_no_deletions_no_db_update(tmp_path: Path):
    """If every file is too young, we MUST NOT issue an UPDATE."""
    from app.services import janitor as svc

    uuid_new = "33333333-3333-3333-3333-333333333333"
    f = tmp_path / f"{uuid_new}.log"
    f.write_text("recent")
    now = time.time()
    os.utime(f, (now, now))

    db = _FakeDB()
    db.set_rowcount(0)
    result = await svc.cleanup_run_logs(
        db, retention_days=7, run_logs_dir=tmp_path
    )
    assert result.files_scanned == 1
    assert result.files_deleted == 0
    assert result.rows_nulled == 0
    # No statements -- no UPDATE on empty deletion set.
    assert db.statements == []


@pytest.mark.asyncio
async def test_run_logs_ignores_non_uuid_filenames(tmp_path: Path):
    from app.services import janitor as svc

    bogus = tmp_path / "not-a-uuid.log"
    bogus.write_text("nope")
    old = time.time() - 30 * 86400
    os.utime(bogus, (old, old))

    db = _FakeDB()
    result = await svc.cleanup_run_logs(
        db, retention_days=7, run_logs_dir=tmp_path
    )
    assert result.files_scanned == 0
    assert result.files_deleted == 0
    assert bogus.exists()


# ---------------------------------------------------------------------------
# cleanup_ingested_order_results
# ---------------------------------------------------------------------------


def _mk_find_stdout(files: list[tuple[float, str]]) -> str:
    """Compose ``find -printf '%T@ %f\\n'`` output for the given files."""
    return "\n".join(f"{epoch} {name}" for epoch, name in files)


@pytest.mark.asyncio
async def test_order_results_skips_when_cursor_unset():
    """If nothing has been ingested yet, the cleanup MUST be a no-op."""
    from app.services import janitor as svc
    from app.models.runs import IngestCursor
    from app.models.servers import Server
    from app.models.stacks import AgentStack

    stack_id = uuid4()
    server_id = uuid4()
    stack = MagicMock()
    stack.id = stack_id
    stack.server_id = server_id
    stack.agent_id = uuid4()
    stack.stack_dir = "/root/seller-market/agents/abc"
    server = MagicMock()
    server.id = server_id

    db = _FakeDB()
    db.set_lookup(AgentStack, stack_id, stack)
    db.set_lookup(Server, server_id, server)
    db.set_lookup(IngestCursor, stack_id, None)  # no cursor

    fake_run = AsyncMock()
    with patch("app.services.ssh.commands.run_command", new=fake_run):
        result = await svc.cleanup_ingested_order_results(
            db, stack_id=stack_id, retention_days=14,
        )

    assert result.files_listed == 0
    assert result.files_deleted == 0
    assert result.errors == []
    # MUST NOT have hit the network.
    fake_run.assert_not_called()


@pytest.mark.asyncio
async def test_order_results_skips_when_last_filename_null():
    """Cursor row exists but last_filename is NULL -- still a no-op."""
    from app.services import janitor as svc
    from app.models.runs import IngestCursor
    from app.models.servers import Server
    from app.models.stacks import AgentStack

    stack_id = uuid4()
    server_id = uuid4()
    stack = MagicMock()
    stack.id = stack_id
    stack.server_id = server_id
    stack.stack_dir = "/root/seller-market/agents/abc"
    server = MagicMock()

    cursor = MagicMock()
    cursor.last_filename = None

    db = _FakeDB()
    db.set_lookup(AgentStack, stack_id, stack)
    db.set_lookup(Server, server_id, server)
    db.set_lookup(IngestCursor, stack_id, cursor)

    fake_run = AsyncMock()
    with patch("app.services.ssh.commands.run_command", new=fake_run):
        result = await svc.cleanup_ingested_order_results(
            db, stack_id=stack_id, retention_days=14,
        )

    assert result.files_deleted == 0
    fake_run.assert_not_called()


@pytest.mark.asyncio
async def test_order_results_filters_uningested_and_unaging():
    """The classic three-file split: 2 deletable, 1 uningested, 0 unaging."""
    from app.services import janitor as svc
    from app.models.runs import IngestCursor
    from app.models.servers import Server
    from app.models.stacks import AgentStack

    stack_id = uuid4()
    server_id = uuid4()
    stack = MagicMock()
    stack.id = stack_id
    stack.server_id = server_id
    stack.stack_dir = "/root/seller-market/agents/abc"
    server = MagicMock()
    server.id = server_id

    cursor = MagicMock()
    cursor.last_filename = "a_b_20260101_080000.json"

    db = _FakeDB()
    db.set_lookup(AgentStack, stack_id, stack)
    db.set_lookup(Server, server_id, server)
    db.set_lookup(IngestCursor, stack_id, cursor)

    # "Now" mocked to 2026-02-15; retention 30d -> cutoff 2026-01-16.
    fake_now = datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()

    old_ingested_a_epoch = datetime(
        2025, 12, 1, 8, 0, 0, tzinfo=timezone.utc
    ).timestamp()
    old_ingested_b_epoch = datetime(
        2025, 12, 1, 12, 0, 0, tzinfo=timezone.utc
    ).timestamp()
    recent_uningested_epoch = datetime(
        2026, 2, 1, 8, 0, 0, tzinfo=timezone.utc
    ).timestamp()

    files = [
        (old_ingested_a_epoch, "a_b_20251201_080000.json"),   # old + ingested
        (recent_uningested_epoch, "a_b_20260201_080000.json"),  # uningested
        (old_ingested_b_epoch, "a_b_20251201_120000.json"),   # old + ingested
    ]

    # Each call to run_command returns the right stdout; first call is
    # the find listing, subsequent calls are rm.
    calls: list[str] = []

    async def _fake_run(server, command, *, timeout=30.0, check=False, **kw):
        calls.append(command)
        if command.startswith("find "):
            return _FakeRemoteResult(stdout=_mk_find_stdout(files))
        if command.startswith("rm "):
            return _FakeRemoteResult(stdout="", stderr="", exit_code=0)
        return _FakeRemoteResult()

    with patch("app.services.ssh.commands.run_command", new=_fake_run), \
         patch("app.services.janitor.time.time", return_value=fake_now):
        result = await svc.cleanup_ingested_order_results(
            db, stack_id=stack_id, retention_days=30,
        )

    assert result.files_listed == 3
    assert result.files_deleted == 2
    assert result.files_skipped_uningested == 1
    assert result.files_skipped_unaging == 0
    assert result.errors == []

    # One find + two rm commands.
    rm_calls = [c for c in calls if c.startswith("rm ")]
    assert len(rm_calls) == 2

    # rm command must include the exact composed path, shell-quoted.
    expected_paths = {
        "/root/seller-market/agents/abc/order_results/a_b_20251201_080000.json",
        "/root/seller-market/agents/abc/order_results/a_b_20251201_120000.json",
    }
    seen_paths = set()
    for rm in rm_calls:
        # We expect shlex.quote on a clean ASCII path to be a no-op
        # (no quoting needed), but the literal must still appear.
        for p in expected_paths:
            if p in rm:
                seen_paths.add(p)
    assert seen_paths == expected_paths, (
        f"expected rm to target both old/ingested paths exactly; saw rm calls={rm_calls!r}"
    )

    # Recent file MUST NOT appear in any rm command.
    for rm in rm_calls:
        assert "a_b_20260201_080000.json" not in rm


@pytest.mark.asyncio
async def test_order_results_skips_unaging_files():
    """All files are old enough by filename but too young by mtime."""
    from app.services import janitor as svc
    from app.models.runs import IngestCursor
    from app.models.servers import Server
    from app.models.stacks import AgentStack

    stack_id = uuid4()
    server_id = uuid4()
    stack = MagicMock()
    stack.id = stack_id
    stack.server_id = server_id
    stack.stack_dir = "/root/seller-market/agents/abc"
    server = MagicMock()

    cursor = MagicMock()
    cursor.last_filename = "z_z_99999999_999999.json"  # everything is "ingested"

    db = _FakeDB()
    db.set_lookup(AgentStack, stack_id, stack)
    db.set_lookup(Server, server_id, server)
    db.set_lookup(IngestCursor, stack_id, cursor)

    fake_now = time.time()
    # Two files, both 1 day old -- younger than the 14-day horizon.
    files = [
        (fake_now - 86400, "a_b_20260101_080000.json"),
        (fake_now - 86400 * 2, "a_b_20260101_090000.json"),
    ]

    async def _fake_run(server, command, **kw):
        if command.startswith("find "):
            return _FakeRemoteResult(stdout=_mk_find_stdout(files))
        return _FakeRemoteResult()

    with patch("app.services.ssh.commands.run_command", new=_fake_run), \
         patch("app.services.janitor.time.time", return_value=fake_now):
        result = await svc.cleanup_ingested_order_results(
            db, stack_id=stack_id, retention_days=14,
        )

    assert result.files_listed == 2
    assert result.files_skipped_unaging == 2
    assert result.files_deleted == 0
    assert result.errors == []


@pytest.mark.asyncio
async def test_order_results_rejects_unsafe_filename_from_find():
    """If find emits a malicious filename, the janitor MUST refuse to rm it."""
    from app.services import janitor as svc
    from app.models.runs import IngestCursor
    from app.models.servers import Server
    from app.models.stacks import AgentStack

    stack_id = uuid4()
    server_id = uuid4()
    stack = MagicMock()
    stack.id = stack_id
    stack.server_id = server_id
    stack.stack_dir = "/root/seller-market/agents/abc"
    server = MagicMock()

    cursor = MagicMock()
    cursor.last_filename = "zzz.json"  # cursor permissive

    db = _FakeDB()
    db.set_lookup(AgentStack, stack_id, stack)
    db.set_lookup(Server, server_id, server)
    db.set_lookup(IngestCursor, stack_id, cursor)

    fake_now = time.time()
    old = fake_now - 30 * 86400
    files = [
        (old, "../../etc/passwd"),
        (old, "x;rm -rf /.json"),
        (old, "good_file.json"),
    ]

    rm_targets: list[str] = []

    async def _fake_run(server, command, **kw):
        if command.startswith("find "):
            return _FakeRemoteResult(stdout=_mk_find_stdout(files))
        if command.startswith("rm "):
            rm_targets.append(command)
            return _FakeRemoteResult(exit_code=0)
        return _FakeRemoteResult()

    with patch("app.services.ssh.commands.run_command", new=_fake_run), \
         patch("app.services.janitor.time.time", return_value=fake_now):
        result = await svc.cleanup_ingested_order_results(
            db, stack_id=stack_id, retention_days=14,
        )

    # Only the well-formed filename should ever reach rm.
    assert len(rm_targets) == 1
    assert "good_file.json" in rm_targets[0]
    # The two malicious entries produced errors.
    assert len(result.errors) == 2
    assert result.files_deleted == 1


@pytest.mark.asyncio
async def test_order_results_handles_ssh_list_failure():
    """SSHError on the listing call surfaces as an error string, not a raise."""
    from app.services import janitor as svc
    from app.models.runs import IngestCursor
    from app.models.servers import Server
    from app.models.stacks import AgentStack
    from app.services.ssh.exceptions import SSHConnectionError

    stack_id = uuid4()
    server_id = uuid4()
    stack = MagicMock()
    stack.id = stack_id
    stack.server_id = server_id
    stack.stack_dir = "/root/seller-market/agents/abc"
    server = MagicMock()
    cursor = MagicMock()
    cursor.last_filename = "anything.json"

    db = _FakeDB()
    db.set_lookup(AgentStack, stack_id, stack)
    db.set_lookup(Server, server_id, server)
    db.set_lookup(IngestCursor, stack_id, cursor)

    async def _fake_run(server, command, **kw):
        raise SSHConnectionError("connection refused")

    with patch("app.services.ssh.commands.run_command", new=_fake_run):
        result = await svc.cleanup_ingested_order_results(
            db, stack_id=stack_id, retention_days=14,
        )

    assert result.files_deleted == 0
    assert result.errors and "ssh list failed" in result.errors[0]


@pytest.mark.asyncio
async def test_order_results_refuses_suspicious_stack_dir():
    """A stack with ``stack_dir='/root'`` must short-circuit before any SSH."""
    from app.services import janitor as svc
    from app.models.runs import IngestCursor
    from app.models.servers import Server
    from app.models.stacks import AgentStack

    stack_id = uuid4()
    server_id = uuid4()
    stack = MagicMock()
    stack.id = stack_id
    stack.server_id = server_id
    stack.stack_dir = "/root"  # <- the trap
    server = MagicMock()
    cursor = MagicMock()
    cursor.last_filename = "x.json"

    db = _FakeDB()
    db.set_lookup(AgentStack, stack_id, stack)
    db.set_lookup(Server, server_id, server)
    db.set_lookup(IngestCursor, stack_id, cursor)

    fake_run = AsyncMock()
    with patch("app.services.ssh.commands.run_command", new=fake_run):
        result = await svc.cleanup_ingested_order_results(
            db, stack_id=stack_id, retention_days=14,
        )

    assert result.files_deleted == 0
    assert result.errors, "expected an error for suspicious stack_dir"
    assert "suspicious" in result.errors[0]
    fake_run.assert_not_called()
