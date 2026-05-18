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


class _FakeAsyncSession:
    """Tiny ``AsyncSession``-shaped object — only ``get`` is exercised below."""

    def __init__(self, run_to_return) -> None:
        self._run = run_to_return
        self.added: list = []
        self.executed: list = []
        self.committed: int = 0

    async def get(self, model, key):  # noqa: D401 — mirror the real signature
        return self._run

    async def execute(self, stmt):
        self.executed.append(stmt)

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
