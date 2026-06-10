"""Unit tests for the Phase-6 runs service (visibility + log reader).

We deliberately test only the two pieces that don't need a DB or SSH:

* :func:`app.services.runs.can_user_see_run` — pure permission check
  the route layer calls AFTER :func:`get_run` to gate the response.
* :func:`app.services.runs.read_run_log` — disk reader for the
  archived stdout/stderr blob, with graceful degradation on missing
  files (so the UI shows "no log captured" instead of a 500).

We do NOT exercise :func:`finalize_run` here. The full path
involves: (1) ``await db.get(Run, ...)``, (2) a ``Path.mkdir`` on
``get_settings().run_logs_dir``, (3) a ``write_bytes`` + ``chmod``,
(4) a SHA256, (5) two DB writes (the row mutation + an audit row).
Pinning all five requires a thorough mock plus a temp dir, and the
critical bits — the SHA256 and the byte-for-byte round-trip — are
already covered by the round-trip test in
:func:`test_read_run_log_returns_bytes` (which proves the reader
half) plus the integration tests in Phase-6 that run the full
:func:`start_run` → executor → :func:`finalize_run` → log fetch
chain against a real session.

All tests use lightweight :class:`SimpleNamespace` stand-ins for
:class:`User` and :class:`Run` — both functions only read a couple
of attributes (``role`` / ``id`` and ``log_blob_ref`` / ``id``
respectively), so a full ORM instance with ``_sa_instance_state``
would add ceremony without coverage.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.runs import can_user_see_run, force_kill_run, read_run_log


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_user(*, role: str, user_id: uuid.UUID | None = None) -> SimpleNamespace:
    """Minimal :class:`User` stand-in — only ``role`` and ``id`` matter."""
    return SimpleNamespace(
        id=user_id or uuid.uuid4(),
        role=role,
    )


def _fake_run(
    *,
    agent_id: uuid.UUID | None = None,
    log_blob_ref: str | None = None,
) -> SimpleNamespace:
    """Minimal :class:`Run` stand-in.

    ``id`` is populated only because :func:`read_run_log` uses it in
    the WARNING log line if the read fails.
    """
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id or uuid.uuid4(),
        log_blob_ref=log_blob_ref,
    )


# ---------------------------------------------------------------------------
# 1. test_can_user_see_run_admin_sees_all
# ---------------------------------------------------------------------------


def test_can_user_see_run_admin_sees_all() -> None:
    """Admins see every run regardless of ``agent_id`` ownership.

    Pins the "admin bypasses the agent-ownership check" branch —
    important because the run-history UI doesn't filter by actor for
    admins, and a regression here would either over-share (admin
    sees everything is fine) or under-share (admin can't see other
    agents' runs, breaking the fleet ops workflow).
    """
    admin = _fake_user(role="admin")
    # A run owned by some other (agent) user.
    other_run = _fake_run(agent_id=uuid.uuid4())

    assert can_user_see_run(admin, other_run) is True


# ---------------------------------------------------------------------------
# 2. test_can_user_see_run_agent_sees_own
# ---------------------------------------------------------------------------


def test_can_user_see_run_agent_sees_own() -> None:
    """An agent sees a run where ``run.agent_id == user.id``.

    ``agent_id`` on the Run row is the stack-owner identity (whose
    orders are at stake), not the human who clicked "Run". A run
    kicked off by an admin on behalf of an agent stack still belongs
    to that agent for visibility purposes.
    """
    agent_id = uuid.uuid4()
    agent = _fake_user(role="agent", user_id=agent_id)
    own_run = _fake_run(agent_id=agent_id)

    assert can_user_see_run(agent, own_run) is True


# ---------------------------------------------------------------------------
# 3. test_can_user_see_run_agent_blocked_for_others
# ---------------------------------------------------------------------------


def test_can_user_see_run_agent_blocked_for_others() -> None:
    """An agent cannot see another agent's run.

    Critical multi-tenant boundary — without this, agent X could
    enumerate agent Y's run history via the public detail endpoint.
    """
    agent_a = _fake_user(role="agent", user_id=uuid.uuid4())
    agent_b_run = _fake_run(agent_id=uuid.uuid4())

    assert can_user_see_run(agent_a, agent_b_run) is False


# ---------------------------------------------------------------------------
# 4. test_read_run_log_missing_file_returns_empty
# ---------------------------------------------------------------------------


async def test_read_run_log_missing_file_returns_empty(tmp_path: Path) -> None:
    """A non-existent ``log_blob_ref`` path returns ``b""`` (not raises).

    Files can vanish between finalize and read (operator cleanup,
    backup restore from a state that pre-dated the run, etc.). The
    UI degrades to "no log captured" instead of 500ing.
    """
    missing = tmp_path / "does-not-exist.log"
    assert not missing.exists()  # sanity

    run = _fake_run(log_blob_ref=str(missing))
    result = await read_run_log(run)

    assert result == b""


# ---------------------------------------------------------------------------
# 5. test_read_run_log_returns_bytes
# ---------------------------------------------------------------------------


async def test_read_run_log_returns_bytes(tmp_path: Path) -> None:
    """An existing file is read back byte-for-byte.

    Pins the happy path. Includes a NUL byte and a non-ASCII suffix to
    catch any well-meaning encoding conversion creeping into the
    reader (the contract is "raw bytes, no transcoding").
    """
    log_path = tmp_path / "run.log"
    payload = b"line one\n\x00binary middle\nfinal line: \xff\xfe"
    log_path.write_bytes(payload)

    run = _fake_run(log_blob_ref=str(log_path))
    result = await read_run_log(run)

    assert result == payload


# ---------------------------------------------------------------------------
# 6. test_read_run_log_none_ref_returns_empty
# ---------------------------------------------------------------------------


async def test_read_run_log_none_ref_returns_empty() -> None:
    """``log_blob_ref=None`` (run still running / never finalized) → ``b""``.

    The Run row has ``log_blob_ref`` populated only by
    :func:`finalize_run`. Mid-run reads (the UI may poll while the
    executor is still streaming) MUST NOT explode on the ``None``.
    """
    run = _fake_run(log_blob_ref=None)
    result = await read_run_log(run)

    assert result == b""


# ---------------------------------------------------------------------------
# Note on finalize_run: deliberately skipped — see module docstring.
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "finalize_run end-to-end requires a mocked AsyncSession + the "
        "settings.run_logs_dir override + a real tmp dir. The byte-level "
        "round-trip is already proven via read_run_log; the audit/commit "
        "wiring is covered by the Phase-6 integration tests against a "
        "real session. Pinning the full mock here would mostly assert on "
        "the mock setup itself, not the production behaviour."
    )
)
async def test_finalize_run_writes_log_with_sha256() -> None:
    """Placeholder for the finalize_run end-to-end test — see reason above."""
    raise AssertionError("intentionally skipped")


# ---------------------------------------------------------------------------
# force_kill_run — branch tests only
# ---------------------------------------------------------------------------
#
# Like finalize_run, the happy path mutates a row + writes an audit + does a
# delete + commits; covering it in unit tests would mostly assert on the
# AsyncSession mock setup. The integration path is exercised via the admin
# /force-kill route in the post-deploy smoke tests. Here we pin the two
# refuse-paths the route layer relies on.


class _FakeResult:
    """Just enough of a SQLAlchemy ``Result`` to back the SELECT path.

    ``scalar_one_or_none()`` returns the stored row; everything else
    raises so an unexpected access surfaces in the test rather than
    silently degrading.
    """

    def __init__(self, row) -> None:
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeAsyncSession:
    """Tiny ``AsyncSession``-shaped object covering both the read paths
    (``execute(SELECT)`` → scalar_one_or_none, ``get(Model, pk)``) AND the
    mutation paths (``execute(DELETE)``, ``add``, ``commit``, ``refresh``).

    The happy-path test inspects ``executed`` to assert the lock DELETE
    actually ran, and ``added`` to assert the AuditLog row went in.
    """

    def __init__(self, run_to_return) -> None:
        self._run = run_to_return
        self.added: list = []
        self.executed: list = []
        self.committed: int = 0

    async def get(self, model, key):  # noqa: D401 — mirror the real signature
        return self._run

    async def execute(self, stmt):
        self.executed.append(stmt)
        # SELECTs route through scalar_one_or_none → return the row.
        # DELETEs ignore the result; returning the same fake is safe.
        return _FakeResult(self._run)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1

    async def refresh(self, obj):
        return obj


@pytest.mark.asyncio
async def test_force_kill_run_refuses_terminal_status() -> None:
    """Force-kill on an already-finished row raises ``ValueError``.

    The admin route translates the ValueError to HTTP 400 — without this
    refusal the recovery action could clobber a successful run's
    ``finished_at`` / ``exit_code`` and corrupt the audit trail.
    """
    run = SimpleNamespace(
        id=uuid.uuid4(),
        stack_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        status="success",  # already terminal
        exit_code=0,
        finished_at=None,
        job_name="cache_warmup",
        trigger="manual",
    )
    db = _FakeAsyncSession(run)

    with pytest.raises(ValueError, match="terminal status"):
        await force_kill_run(db, run_id=run.id, actor_id=uuid.uuid4())

    # Confirm we didn't mutate the row.
    assert run.status == "success"
    assert db.committed == 0


@pytest.mark.asyncio
async def test_force_kill_run_missing_row_raises_lookup() -> None:
    """Unknown run id raises ``LookupError`` (route → 404).

    Distinct from the terminal-status branch so the route can return
    different HTTP codes (404 vs 400) and so a bug in the SELECT path
    is caught separately from a bug in the status check.
    """
    db = _FakeAsyncSession(None)  # db.get returns None

    with pytest.raises(LookupError):
        await force_kill_run(db, run_id=uuid.uuid4(), actor_id=uuid.uuid4())

    assert db.committed == 0


@pytest.mark.asyncio
async def test_force_kill_run_stale_running_row_transitions_to_killed() -> None:
    """Happy path: stale `running` row → killed + lock deleted + audit + commit.

    The refusal branches above prove we don't clobber terminal rows.
    This one pins the actual recovery path the admin button relies on:
    every state-change the docstring promises must land.
    """
    from app.models.audit import AuditLog
    from sqlalchemy.sql.dml import Delete as DeleteStmt
    from sqlalchemy.sql.selectable import Select as SelectStmt

    run_id = uuid.uuid4()
    stack_id = uuid.uuid4()
    run = SimpleNamespace(
        id=run_id,
        stack_id=stack_id,
        agent_id=uuid.uuid4(),
        status="running",
        exit_code=None,
        finished_at=None,
        job_name="cache_warmup",
        trigger="manual",
    )
    db = _FakeAsyncSession(run)
    actor = uuid.uuid4()

    returned = await force_kill_run(db, run_id=run_id, actor_id=actor)

    # 1. Row is now killed with synthetic exit_code and a finished_at.
    assert returned is run
    assert run.status == "killed"
    assert run.exit_code == -1
    assert run.finished_at is not None

    # 2. Two statements were issued:
    #    a) the locked SELECT against the runs row,
    #    b) the DELETE against stack_run_locks scoped to (stack_id, run_id).
    assert len(db.executed) == 2
    select_stmt, delete_stmt = db.executed
    assert isinstance(select_stmt, SelectStmt)
    assert isinstance(delete_stmt, DeleteStmt)
    # SQLAlchemy's compiled DELETE doesn't expose the WHERE columns
    # directly without binding params, but the underlying table is
    # ``stack_run_locks`` — confirm we're not targeting the wrong table
    # by accident. (Compare by name, not identity: SQLAlchemy may copy
    # Table metadata between module-load cycles in test isolation.)
    assert delete_stmt.table.name == "stack_run_locks"

    # 3. One AuditLog row was added with the recovery-specific action.
    audit_rows = [o for o in db.added if isinstance(o, AuditLog)]
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit.action == "run.force_kill"
    assert audit.target_type == "run"
    assert audit.target_id == str(run_id)
    assert audit.actor_user_id == actor
    # Before-snapshot captured the original state; after-snapshot the new one.
    assert audit.before_json["status"] == "running"
    assert audit.after_json["status"] == "killed"

    # 4. Exactly one commit happened — multiple commits would mean we
    #    accidentally interleaved the lock DELETE with the row UPDATE.
    assert db.committed == 1


# ---------------------------------------------------------------------------
# read_run_log — gz transparency (full logs are stored gzip-compressed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_run_log_decompresses_gz_blob(tmp_path: Path) -> None:
    """A ``.log.gz`` blob is returned decompressed (callers see plain text)."""
    import gzip

    payload = b"line one\nline two\n" * 100
    log_path = tmp_path / "run.log.gz"
    log_path.write_bytes(gzip.compress(payload))

    run = _fake_run(log_blob_ref=str(log_path))
    assert await read_run_log(run) == payload


@pytest.mark.asyncio
async def test_read_run_log_corrupt_gz_returns_empty(tmp_path: Path) -> None:
    """Corrupt gzip degrades to b'' — same contract as a missing file."""
    log_path = tmp_path / "run.log.gz"
    log_path.write_bytes(b"definitely not gzip")

    run = _fake_run(log_blob_ref=str(log_path))
    assert await read_run_log(run) == b""


# ---------------------------------------------------------------------------
# read_run_log_tail — bounded inline render for the run-detail page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_run_log_tail_small_plain_file(tmp_path: Path) -> None:
    from app.services.runs import read_run_log_tail

    log_path = tmp_path / "run.log"
    log_path.write_bytes(b"short log")
    run = _fake_run(log_blob_ref=str(log_path))

    tail, total = await read_run_log_tail(run, max_bytes=1024)
    assert tail == b"short log"
    assert total == len(b"short log")


@pytest.mark.asyncio
async def test_read_run_log_tail_large_plain_file_seeks(tmp_path: Path) -> None:
    """Only the LAST max_bytes come back; total reports the real size."""
    from app.services.runs import read_run_log_tail

    payload = b"A" * 5000 + b"THE-END"
    log_path = tmp_path / "run.log"
    log_path.write_bytes(payload)
    run = _fake_run(log_blob_ref=str(log_path))

    tail, total = await read_run_log_tail(run, max_bytes=100)
    assert len(tail) == 100
    assert tail.endswith(b"THE-END")
    assert total == len(payload)


@pytest.mark.asyncio
async def test_read_run_log_tail_gz_streams_and_counts(tmp_path: Path) -> None:
    """Gz blobs: tail of the DECOMPRESSED text + exact decompressed total."""
    import gzip

    from app.services.runs import read_run_log_tail

    payload = b"B" * 300_000 + b"FINAL-LINE"
    log_path = tmp_path / "run.log.gz"
    log_path.write_bytes(gzip.compress(payload))
    run = _fake_run(log_blob_ref=str(log_path))

    tail, total = await read_run_log_tail(run, max_bytes=64)
    assert len(tail) == 64
    assert tail.endswith(b"FINAL-LINE")
    assert total == len(payload)


@pytest.mark.asyncio
async def test_read_run_log_tail_missing_and_corrupt(tmp_path: Path) -> None:
    from app.services.runs import read_run_log_tail

    missing = _fake_run(log_blob_ref=str(tmp_path / "nope.log"))
    assert await read_run_log_tail(missing) == (b"", 0)

    corrupt_path = tmp_path / "bad.log.gz"
    corrupt_path.write_bytes(b"not gzip at all")
    corrupt = _fake_run(log_blob_ref=str(corrupt_path))
    assert await read_run_log_tail(corrupt) == (b"", 0)

    no_ref = _fake_run(log_blob_ref=None)
    assert await read_run_log_tail(no_ref) == (b"", 0)
