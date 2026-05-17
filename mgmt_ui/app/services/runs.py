"""Run lifecycle service (Phase 6).

Owns the durable state of the ``runs`` table: insert a row in
``running`` state on start, mutate it to a terminal state on finalize,
and read it back for listing / detail / log replay.

The actual SSH ``docker exec`` and live log streaming live in
:mod:`app.services.run_executor` (parallel agent B). This module is
deliberately I/O-bound only on the DB and the local log archive — no
SSH, no per-stack mutex (that's :mod:`app.services.run_locks`), no
scheduler suppression (that's :mod:`app.services.scheduler_snapshot`).
Callers stitch the four together.

Log archival
------------
On :func:`finalize_run` we write the captured stdout+stderr bytes to
``<run_logs_dir>/<run_id>.log`` with mode ``0600``, hash it with
SHA-256, and store the absolute path + hex digest on the row. The chmod
is best-effort: Windows dev machines silently swallow it. Production
runs on Linux where the file ends up readable only by the mgmt service
account.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.runs import Run
from app.models.users import User
from app.schemas.run import JobName, RunStatus, RunTrigger
from app.settings import get_settings

logger = logging.getLogger(__name__)


def _public_snapshot(run: Run) -> dict:
    """JSON-safe projection of a :class:`Run` row for audit payloads.

    We stringify the UUIDs because ``audit_log.before_json`` /
    ``after_json`` are ``JSONB`` and Python's ``json`` module doesn't
    know how to serialise :class:`uuid.UUID`. The log paths and SHA are
    intentionally omitted — they're operational metadata, not part of
    the user-facing run state.
    """
    return {
        "id": str(run.id),
        "stack_id": str(run.stack_id),
        "agent_id": str(run.agent_id),
        "job_name": run.job_name,
        "trigger": run.trigger,
        "status": run.status,
        "exit_code": run.exit_code,
    }


async def _write_audit(
    db: AsyncSession,
    *,
    actor_id: Optional[UUID],
    action: str,
    target_id: UUID,
    before: Optional[dict],
    after: Optional[dict],
) -> None:
    """Insert one ``audit_log`` row for a run-lifecycle mutation.

    ``target_type`` is always ``"run"`` here. Mirrors the pattern in
    :mod:`app.services.scheduler_jobs` and :mod:`app.services.stacks`
    so audit subscribers can rely on a consistent shape.
    """
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action=action,
            target_type="run",
            target_id=str(target_id),
            before_json=before,
            after_json=after,
            ts=datetime.now(timezone.utc),
        )
    )


async def start_run(
    db: AsyncSession,
    *,
    stack_id: UUID,
    agent_id: UUID,
    job_name: JobName,
    trigger: RunTrigger,
    actor_id: Optional[UUID],
) -> Run:
    """Insert a fresh :class:`Run` row in ``running`` state.

    The caller MUST have already acquired the stack_run_locks row via
    :func:`app.services.run_locks.acquire_lock` — start_run does not
    take the lock itself, because callers also typically wrap the run
    in a scheduler-snapshot context that needs to happen before any
    durable state is written.

    Commits immediately so the row is visible to other transactions
    (e.g. a parallel ``GET /runs`` while the executor is still streaming
    stdout). Returns the refreshed ORM object with server-defaulted
    ``id`` / ``started_at`` populated.
    """
    now = datetime.now(timezone.utc)
    run = Run(
        stack_id=stack_id,
        agent_id=agent_id,
        job_name=job_name,
        trigger=trigger,
        started_at=now,
        finished_at=None,
        status="running",
        exit_code=None,
        log_blob_ref=None,
        log_blob_sha256=None,
    )
    db.add(run)
    await db.flush()
    await _write_audit(
        db,
        actor_id=actor_id,
        action="run.start",
        target_id=run.id,
        before=None,
        after=_public_snapshot(run),
    )
    await db.commit()
    await db.refresh(run)
    return run


async def finalize_run(
    db: AsyncSession,
    *,
    run_id: UUID,
    status: RunStatus,
    exit_code: Optional[int],
    captured_log: bytes,
    actor_id: Optional[UUID],
) -> Run:
    """Mark a :class:`Run` finished, archive its captured log, audit.

    Args:
        db: Session — committed at the end so the terminal state is
            visible immediately.
        run_id: PK of the row to finalize. Raises :class:`LookupError`
            if the row was deleted out from under us.
        status: Terminal status — one of ``success`` / ``failed`` /
            ``killed``. ``running`` would be a programming error;
            ``RunStatus`` is a :class:`Literal` so the type checker
            catches it.
        exit_code: Process exit code if known; ``None`` when the
            executor never observed one (e.g. SSH dropped mid-run).
        captured_log: Full combined stdout+stderr bytes. Written
            verbatim to disk — no trimming, no encoding conversion.
        actor_id: User on whose behalf the finalize is happening, for
            the audit row. Usually the same actor as ``start_run``.

    Notes:
        We chmod the log file to ``0600`` on POSIX; the OSError swallow
        is for Windows dev where chmod is a best-effort no-op.

        The audit ``action`` is ``run.complete`` on success and
        ``run.fail`` on every non-success terminal state (failed /
        killed) — the latter aggregates both because operators tend to
        page on "any non-clean exit" rather than the specific reason.
    """
    run = await db.get(Run, run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    before = _public_snapshot(run)

    log_dir = Path(get_settings().run_logs_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run.id}.log"
    sha = hashlib.sha256(captured_log).hexdigest()
    log_path.write_bytes(captured_log)
    try:
        log_path.chmod(0o600)
    except OSError:
        # Windows dev: chmod is best-effort. Production is Linux.
        pass

    run.status = status
    run.exit_code = exit_code
    run.finished_at = datetime.now(timezone.utc)
    run.log_blob_ref = str(log_path)
    run.log_blob_sha256 = sha

    action = "run.complete" if status == "success" else "run.fail"
    await _write_audit(
        db,
        actor_id=actor_id,
        action=action,
        target_id=run.id,
        before=before,
        after=_public_snapshot(run),
    )
    await db.commit()
    await db.refresh(run)
    return run


async def get_run(db: AsyncSession, run_id: UUID) -> Optional[Run]:
    """Fetch one run by PK. ``None`` if missing."""
    return await db.get(Run, run_id)


async def list_runs(
    db: AsyncSession,
    *,
    agent_id: Optional[UUID] = None,
    stack_id: Optional[UUID] = None,
    status: Optional[RunStatus] = None,
    limit: int = 100,
) -> list[Run]:
    """List runs newest-first, optionally filtered by agent / stack / status.

    The ``limit`` defaults to 100 because the UI's run history pane is
    paginated at that boundary; callers asking for "all runs for stack
    X" should explicitly raise the cap.
    """
    stmt = select(Run).order_by(desc(Run.started_at)).limit(limit)
    if agent_id is not None:
        stmt = stmt.where(Run.agent_id == agent_id)
    if stack_id is not None:
        stmt = stmt.where(Run.stack_id == stack_id)
    if status is not None:
        stmt = stmt.where(Run.status == status)
    result = await db.execute(stmt)
    return list(result.scalars().all())


def can_user_see_run(user: User, run: Run) -> bool:
    """Permission check: admins see all runs, agents see only their own.

    Pure function — no DB, no I/O. The route layer calls this AFTER
    :func:`get_run` to gate the response. We compare on ``agent_id``
    rather than ``actor_id`` because ``agent_id`` is the stack-owner
    identity (whose orders are at stake), not the human who clicked
    "Run". A run kicked off by an admin on behalf of an agent stack
    still belongs to that agent for visibility purposes.
    """
    return user.role == "admin" or run.agent_id == user.id


async def read_run_log(run: Run) -> bytes:
    """Read the archived log bytes from disk.

    Returns ``b""`` on missing file or IO error rather than raising —
    the UI degrades to "no log captured" gracefully. We do not verify
    the on-disk SHA against ``run.log_blob_sha256`` here; that's a
    separate integrity check the operator can run via a CLI when
    investigating suspicious behaviour.
    """
    if not run.log_blob_ref:
        return b""
    try:
        p = Path(run.log_blob_ref)
        if not p.exists():
            return b""
        return p.read_bytes()
    except OSError as exc:
        logger.warning("read_run_log failed run=%s: %s", run.id, exc)
        return b""


__all__ = [
    "start_run",
    "finalize_run",
    "get_run",
    "list_runs",
    "can_user_see_run",
    "read_run_log",
]
