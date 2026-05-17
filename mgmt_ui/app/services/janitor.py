"""Janitor cleanup helpers.

Three pure cleanup primitives, each idempotent and safe to run on any
cadence:

* :func:`cleanup_ingested_order_results` -- rm old order_results JSONs on
  the trading server, BUT only ones already ingested past
  ``ingest_cursors.last_filename``.
* :func:`cleanup_run_logs` -- delete archived run-log files on the mgmt
  VPS that are older than the retention horizon, and NULL out the
  corresponding ``runs.log_blob_ref`` so the UI doesn't promise content
  that no longer exists.
* :func:`cleanup_old_health_signals` -- DELETE rows from health_signals
  where ts < cutoff AND ack_at IS NOT NULL (un-acked signals stay).

The orchestrator :func:`run_janitor_tick` runs all three with telemetry
that the worker logs.

Design notes
------------
* Path safety is paranoid: the bot's order_results filename grammar is
  ASCII alnum + ``._-`` plus a literal ``.json`` suffix. Anything else
  is rejected by :func:`_is_safe_filename`. Belt-and-braces,
  :func:`_assert_rm_target_in_scope` then refuses any composed path
  that doesn't have ``<stack_dir>/order_results/`` as a strict prefix
  and refuses dangerous ``stack_dir`` values (``/``, ``/root``,
  ``/home``).
* SSH / OS errors are caught at the per-file level and accumulated
  into the returned dataclass so a single bad stack or file can't
  wedge the surrounding loop.
* :func:`run_command` and ``sftp_*`` helpers are imported lazily to
  keep paramiko out of the module-load path (tests stub the SSH layer).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health import HealthSignal
from app.models.runs import IngestCursor, Run
from app.models.servers import Server
from app.models.stacks import AgentStack
from app.services.ssh.exceptions import PathOutOfScopeError, SSHError

logger = logging.getLogger(__name__)


# Default retention horizons -- also baked into app.settings so admins
# can tune them via env vars without touching code (Agent D adds those).
DEFAULT_ORDER_RESULTS_RETENTION_DAYS = 14
DEFAULT_RUN_LOG_RETENTION_DAYS = 90
DEFAULT_HEALTH_SIGNAL_RETENTION_DAYS = 30


# ---------------------------------------------------------------------------
# Telemetry dataclasses
# ---------------------------------------------------------------------------


@dataclass
class OrderResultsCleanupResult:
    """Per-stack result of an order_results cleanup pass."""

    stack_id: UUID
    files_listed: int = 0
    files_deleted: int = 0
    files_skipped_unaging: int = 0  # too young to delete
    files_skipped_uningested: int = 0  # filename > last_filename
    errors: list[str] = field(default_factory=list)


@dataclass
class RunLogCleanupResult:
    """Aggregate result of a run-log cleanup pass on the mgmt VPS."""

    files_scanned: int = 0
    files_deleted: int = 0
    rows_nulled: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class HealthSignalCleanupResult:
    """Aggregate result of a health_signals DELETE pass."""

    rows_deleted: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class JanitorTickResult:
    """One run of the orchestrator -- all three cleanup primitives."""

    order_results: list[OrderResultsCleanupResult] = field(default_factory=list)
    run_logs: RunLogCleanupResult = field(default_factory=RunLogCleanupResult)
    health_signals: HealthSignalCleanupResult = field(
        default_factory=HealthSignalCleanupResult
    )


# ---------------------------------------------------------------------------
# Path / filename safety
# ---------------------------------------------------------------------------


def _is_safe_filename(name: str) -> bool:
    """Conservative whitelist for an order_results filename.

    The bot writes ``<username>_<broker>_<YYYYMMDD_HHMMSS>.json``. We
    accept ASCII alnum + ``._-`` and a literal ``.json`` suffix.
    Anything outside that -- shell metas, unicode, path separators,
    leading dots, dot-dot -- is rejected so we can never expand
    arbitrary FS paths.
    """
    if not name or len(name) > 256:
        return False
    if name in (".", "..") or name.startswith("."):
        return False
    if "/" in name or "\\" in name:
        return False
    return bool(re.match(r"^[A-Za-z0-9._-]+\.json$", name))


def _assert_rm_target_in_scope(stack: AgentStack, full_path: str) -> None:
    """Refuse any rm target that isn't strictly under ``<stack_dir>/order_results/``.

    Belt-and-braces above :func:`_is_safe_filename` -- even if filename
    validation is bypassed somehow, we'll trip on this.

    Raises:
        PathOutOfScopeError: if the composed path falls outside the
            allowed prefix, the ``stack_dir`` itself is suspicious, or
            the path contains a ``..`` segment.
    """
    base = (stack.stack_dir or "").rstrip("/")
    if not base or base in ("/", "/root", "/home"):
        raise PathOutOfScopeError(
            f"refusing janitor on suspicious stack_dir={base!r}"
        )
    expected_prefix = f"{base}/order_results/"
    if not full_path.startswith(expected_prefix):
        raise PathOutOfScopeError(
            f"refusing rm: {full_path!r} is not under {expected_prefix!r}"
        )
    # Belt-and-braces: no .. segments anywhere in the path.
    if ".." in full_path.split("/"):
        raise PathOutOfScopeError(
            f"refusing rm: {full_path!r} contains '..'"
        )


# ---------------------------------------------------------------------------
# 1. Remote order_results cleanup
# ---------------------------------------------------------------------------


async def cleanup_ingested_order_results(
    db: AsyncSession,
    *,
    stack_id: UUID,
    retention_days: int = DEFAULT_ORDER_RESULTS_RETENTION_DAYS,
) -> OrderResultsCleanupResult:
    """Delete already-ingested order_results JSON files older than the horizon.

    Only files with filename ``<= cursor.last_filename`` AND mtime older
    than ``retention_days`` are removed -- everything else stays so the
    ingestor still gets a chance to see it.

    Never raises operationally: SSH / path / OS errors are caught and
    accumulated into the returned dataclass.
    """
    # Lazy import keeps paramiko off the module-load path; tests patch
    # ``app.services.ssh.commands.run_command`` at the source.
    from app.services.ssh.commands import run_command

    result = OrderResultsCleanupResult(stack_id=stack_id)

    stack = await db.get(AgentStack, stack_id)
    if stack is None:
        result.errors.append(f"stack {stack_id} not found")
        return result
    server = await db.get(Server, stack.server_id)
    if server is None:
        result.errors.append(f"server {stack.server_id} not found")
        return result

    cursor = await db.get(IngestCursor, stack_id)
    if cursor is None or cursor.last_filename is None:
        # Nothing has been ingested yet -- deleting would be premature
        # and could destroy files the ingestor hasn't seen.
        return result
    last_filename = cursor.last_filename

    # Up-front stack_dir sanity check so we never even compose a list
    # command rooted at "/" or "/root".
    base = (stack.stack_dir or "").rstrip("/")
    if not base or base in ("/", "/root", "/home"):
        result.errors.append(
            f"refusing janitor on suspicious stack_dir={base!r}"
        )
        return result

    remote_dir = f"{base}/order_results"
    list_cmd = (
        f"find {shlex.quote(remote_dir)} -maxdepth 1 -type f "
        f"-name '*.json' -printf '%T@ %f\\n' 2>/dev/null || true"
    )
    try:
        listing = await run_command(server, list_cmd, timeout=30.0, check=False)
    except SSHError as exc:
        result.errors.append(f"ssh list failed: {exc}")
        return result

    now_epoch = time.time()
    cutoff_epoch = now_epoch - retention_days * 86400.0

    for line in listing.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            mtime_str, filename = line.split(" ", 1)
            mtime_epoch = float(mtime_str)
        except (ValueError, TypeError):
            # Unparseable find output -- skip; next tick will retry.
            continue

        result.files_listed += 1

        if not _is_safe_filename(filename):
            result.errors.append(f"unsafe filename rejected: {filename!r}")
            continue

        if filename > last_filename:
            # Not yet ingested -- absolutely never delete.
            result.files_skipped_uningested += 1
            continue

        if mtime_epoch > cutoff_epoch:
            # Too young to garbage-collect.
            result.files_skipped_unaging += 1
            continue

        full_path = f"{base}/order_results/{filename}"
        try:
            _assert_rm_target_in_scope(stack, full_path)
        except PathOutOfScopeError as exc:
            result.errors.append(str(exc))
            continue

        rm_cmd = f"rm -f {shlex.quote(full_path)}"
        try:
            rm_result = await run_command(server, rm_cmd, timeout=15.0, check=False)
        except SSHError as exc:
            result.errors.append(f"ssh rm failed for {filename!r}: {exc}")
            continue
        if rm_result.exit_code != 0:
            result.errors.append(
                f"rm exit={rm_result.exit_code} for {filename!r}: "
                f"{rm_result.stderr.strip()[:200]}"
            )
            continue
        result.files_deleted += 1

    logger.info(
        "janitor.order_results stack=%s listed=%d deleted=%d "
        "skipped_unaging=%d skipped_uningested=%d errors=%d",
        stack_id,
        result.files_listed,
        result.files_deleted,
        result.files_skipped_unaging,
        result.files_skipped_uningested,
        len(result.errors),
    )
    return result


# ---------------------------------------------------------------------------
# 2. Local run-log cleanup
# ---------------------------------------------------------------------------


# Strict UUID-4 / generic UUID-shaped filename: 8-4-4-4-12 lowercase hex
# followed by ``.log``. This matches the per-run filename written by the
# run_executor and refuses any stray non-UUID file the operator may have
# dropped into the directory.
_RUN_LOG_NAME_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.log$"
)


async def cleanup_run_logs(
    db: AsyncSession,
    *,
    retention_days: int = DEFAULT_RUN_LOG_RETENTION_DAYS,
    run_logs_dir: Path,
) -> RunLogCleanupResult:
    """Delete run-log files older than the retention horizon; NULL the refs.

    The function is non-recursive: archived run logs live flat in the
    given directory. Each filename must match ``<uuid>.log``; anything
    else is left alone (someone's stray file).

    After the file deletions we run ONE bulk ``UPDATE runs SET
    log_blob_ref = NULL, log_blob_sha256 = NULL WHERE id IN (...)`` so
    the UI doesn't promise content that no longer exists on disk.

    Per-file ``OSError`` is caught and reported in ``errors`` so one
    locked file doesn't abort the whole pass.
    """
    result = RunLogCleanupResult()

    if not run_logs_dir.exists() or not run_logs_dir.is_dir():
        # Nothing to do -- log nothing noisy.
        return result

    cutoff_epoch = time.time() - retention_days * 86400.0
    deleted_uuids: list[UUID] = []

    try:
        entries = list(run_logs_dir.iterdir())
    except OSError as exc:
        result.errors.append(f"iterdir failed: {exc}")
        return result

    for entry in entries:
        if not entry.is_file():
            continue
        m = _RUN_LOG_NAME_RE.match(entry.name)
        if m is None:
            # Foreign file -- leave it alone, don't even count it.
            continue
        result.files_scanned += 1
        try:
            mtime = entry.stat().st_mtime
        except OSError as exc:
            result.errors.append(f"stat failed for {entry.name!r}: {exc}")
            continue
        if mtime > cutoff_epoch:
            # Too young.
            continue
        try:
            entry.unlink()
        except OSError as exc:
            result.errors.append(f"unlink failed for {entry.name!r}: {exc}")
            continue
        result.files_deleted += 1
        try:
            deleted_uuids.append(UUID(m.group(1)))
        except ValueError:
            # Regex guarantees a valid hex pattern, but be paranoid.
            pass

    if deleted_uuids:
        try:
            stmt = (
                update(Run)
                .where(Run.id.in_(deleted_uuids))
                .values(log_blob_ref=None, log_blob_sha256=None)
            )
            db_result = await db.execute(stmt)
            await db.commit()
            # SQLAlchemy returns rowcount or -1 if the dialect can't tell.
            rc = getattr(db_result, "rowcount", 0) or 0
            result.rows_nulled = max(rc, 0)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"runs update failed: {exc}")
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass

    logger.info(
        "janitor.run_logs scanned=%d deleted=%d rows_nulled=%d errors=%d",
        result.files_scanned,
        result.files_deleted,
        result.rows_nulled,
        len(result.errors),
    )
    return result


# ---------------------------------------------------------------------------
# 3. health_signals DELETE
# ---------------------------------------------------------------------------


async def cleanup_old_health_signals(
    db: AsyncSession,
    *,
    retention_days: int = DEFAULT_HEALTH_SIGNAL_RETENTION_DAYS,
) -> HealthSignalCleanupResult:
    """Delete acknowledged health_signal rows older than the cutoff.

    Un-ack'd rows ALWAYS stay -- they represent unresolved conditions
    the operator hasn't seen. Only signals that are both old AND already
    triaged get removed.
    """
    result = HealthSignalCleanupResult()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    stmt = (
        delete(HealthSignal)
        .where(HealthSignal.ts < cutoff)
        .where(HealthSignal.ack_at.is_not(None))
    )
    try:
        db_result = await db.execute(stmt)
        await db.commit()
        rc = getattr(db_result, "rowcount", 0) or 0
        result.rows_deleted = max(rc, 0)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"delete failed: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    logger.info(
        "janitor.health_signals deleted=%d errors=%d",
        result.rows_deleted,
        len(result.errors),
    )
    return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def run_janitor_tick(
    db: AsyncSession,
    *,
    run_logs_dir: Path,
    order_results_retention_days: int = DEFAULT_ORDER_RESULTS_RETENTION_DAYS,
    run_log_retention_days: int = DEFAULT_RUN_LOG_RETENTION_DAYS,
    health_signal_retention_days: int = DEFAULT_HEALTH_SIGNAL_RETENTION_DAYS,
) -> JanitorTickResult:
    """Run all three cleanup primitives once and return a combined report.

    Per-stack order_results cleanup iterates :class:`AgentStack` rows
    sequentially -- there's no parallelism here; the worker controls
    cadence and we keep SSH pressure low.

    Raises:
        ValueError: if any retention value is negative. A negative value
            shifts the cutoff into the future and would purge *every*
            qualifying row — a recoverable typo in a config file
            shouldn't trigger mass deletion. Zero is allowed (delete
            everything older than "now") since some operators legitimately
            run a one-shot cleanup with retention_days=0.
    """
    for name, value in (
        ("order_results_retention_days", order_results_retention_days),
        ("run_log_retention_days", run_log_retention_days),
        ("health_signal_retention_days", health_signal_retention_days),
    ):
        if value < 0:
            raise ValueError(
                f"janitor retention {name}={value!r} must be >= 0; "
                f"refusing to run with a negative retention which would "
                f"shift the cutoff into the future and purge fresh data"
            )

    tick = JanitorTickResult()

    # 1. Order results -- per stack.
    stack_rows = (await db.execute(select(AgentStack))).scalars().all()
    for stack in stack_rows:
        per_stack = await cleanup_ingested_order_results(
            db,
            stack_id=stack.id,
            retention_days=order_results_retention_days,
        )
        tick.order_results.append(per_stack)

    # 2. Local run logs.
    tick.run_logs = await cleanup_run_logs(
        db,
        retention_days=run_log_retention_days,
        run_logs_dir=run_logs_dir,
    )

    # 3. health_signals DELETE.
    tick.health_signals = await cleanup_old_health_signals(
        db,
        retention_days=health_signal_retention_days,
    )

    total_or_listed = sum(r.files_listed for r in tick.order_results)
    total_or_deleted = sum(r.files_deleted for r in tick.order_results)
    total_or_errors = sum(len(r.errors) for r in tick.order_results)
    logger.info(
        "janitor.tick stacks=%d or_listed=%d or_deleted=%d or_errors=%d "
        "log_deleted=%d log_rows_nulled=%d health_deleted=%d",
        len(tick.order_results),
        total_or_listed,
        total_or_deleted,
        total_or_errors,
        tick.run_logs.files_deleted,
        tick.run_logs.rows_nulled,
        tick.health_signals.rows_deleted,
    )
    return tick


__all__ = [
    "DEFAULT_ORDER_RESULTS_RETENTION_DAYS",
    "DEFAULT_RUN_LOG_RETENTION_DAYS",
    "DEFAULT_HEALTH_SIGNAL_RETENTION_DAYS",
    "OrderResultsCleanupResult",
    "RunLogCleanupResult",
    "HealthSignalCleanupResult",
    "JanitorTickResult",
    "cleanup_ingested_order_results",
    "cleanup_run_logs",
    "cleanup_old_health_signals",
    "run_janitor_tick",
]
