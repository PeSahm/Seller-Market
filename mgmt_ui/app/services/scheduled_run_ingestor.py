"""Ingest scheduled-run markers written by the bot's ``scheduler.py``.

Closes the visibility gap described in issue #62: when the trading bot's
in-container scheduler fires a cache_warmup / run_trading at its
configured cron time, it now drops two JSON markers in the stack's
``run_results/`` directory — one when the job starts and one when it
finishes. This service SFTPs them in, parses, and UPSERTs the matching
``runs`` row. The mgmt UI's existing Runs list / detail page then shows
the scheduled fire alongside manual button-clicks.

Marker shape (matches what
``SellerMarket/scheduler.py::_emit_scheduled_run_marker`` writes):

* ``schema_version``: int, currently 1 — bumped on incompatible changes.
* ``scheduled_run_id``: UUID4 string. Becomes the ``runs.id`` so the
  running → terminal transition is a clean UPSERT (no cursor needed).
* ``job_name``: ``"cache_warmup"`` or ``"run_trading"`` — the mgmt UI
  enum. Files with any other value are skipped (the bot only emits
  these two and we want to fail closed on schema drift).
* ``trigger``: always ``"scheduled"``.
* ``started_at`` / ``finished_at``: ISO-8601 UTC.
* ``status``: ``"running"`` for the start marker, ``"success"`` /
  ``"failed"`` for the final marker.
* ``exit_code``: int on the final marker only.
* ``stdout_tail`` / ``stderr_tail``: each capped at 4 KB by the bot.
* ``log_file`` (optional, final markers): filename of the FULL combined
  output, gzip-compressed, written by the bot next to the marker
  (``scheduled_run_<uuid>.log.gz``). When present we fetch it and archive
  the gz verbatim so the operator can download the complete log from the
  Runs page; the 4 KB tails remain the fallback for old bots / fetch
  failures.

Idempotency: every marker is processed at-least-once. We delete the
remote file (and its consumed ``.log.gz``) after a successful UPSERT so
it can't be re-read on the next tick. A failed UPSERT leaves the file in
place; the next tick tries again. A failed FULL-LOG fetch archives the
tails but keeps the marker + gz so later ticks can retry the fetch,
bounded by a 24h window from the run's ``finished_at``.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import shlex
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import hash_lock_key
from app.models.audit import AuditLog
from app.models.runs import Run
from app.models.servers import Server
from app.models.stacks import AgentStack
from app.services.ssh.exceptions import SSHError
from app.settings import get_settings

logger = logging.getLogger(__name__)


# We keep the marker filename pattern strict — the bot writes
# ``scheduled_run_<uuid>.json`` for the final and
# ``scheduled_run_<uuid>.running.json`` for the in-flight start.
# Foreign files dropped into the directory by an operator (debug
# dumps, editor swap files, ...) are ignored.
_FINAL_FILENAME_PREFIX = "scheduled_run_"
_RUNNING_SUFFIX = ".running.json"
_FINAL_SUFFIX = ".json"
_SUPPORTED_SCHEMA = 1
_ALLOWED_JOB_NAMES = {"cache_warmup", "run_trading"}

# Full-log fetch guards: the on-the-wire gz is rejected above 16 MiB and the
# gz must verifiably decompress to <= 256 MiB (gzip-bomb guard) before we
# archive it. Run logs compress ~10-20x, so 16 MiB gz covers any real run.
_MAX_GZ_BYTES = 16 * 1024 * 1024
_MAX_LOG_BYTES = 256 * 1024 * 1024

# When the full-log gz fetch fails we archive the marker tails as a fallback
# but keep the marker + remote gz so later ticks can retry the fetch — bounded
# by this window (measured from the run's finished_at) so a permanently
# unfetchable gz can't make a marker retry forever. After expiry the marker is
# consumed (tails stand) and the orphan gz ages out via the bot's 7-day prune.
_FULL_LOG_RETRY_WINDOW_SECONDS = 24 * 3600


def _full_log_retry_open(payload: dict) -> bool:
    """Whether a failed full-log fetch is still worth retrying."""
    finished = _parse_iso(payload.get("finished_at"))
    if finished is None:
        return False
    age = (datetime.now(timezone.utc) - finished).total_seconds()
    return 0 <= age < _FULL_LOG_RETRY_WINDOW_SECONDS


def _gzip_decompresses_within(data: bytes, max_decompressed: int) -> bool:
    """Stream-verify ``data`` is valid gzip whose payload fits the cap.

    Decompresses in 1 MiB chunks that are immediately discarded — bounded
    memory regardless of payload size.
    """
    try:
        total = 0
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            while True:
                chunk = gz.read(1 << 20)
                if not chunk:
                    return True
                total += len(chunk)
                if total > max_decompressed:
                    return False
    except (OSError, EOFError, zlib.error):
        return False


@dataclass
class IngestResult:
    """Outcome of one stack's ingest tick.

    All counters default to zero so the trade-ingestor-style logging
    line at the worker layer can be uniform.
    """

    stack_id: UUID
    files_seen: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class _RemoteMarker:
    name: str
    full_path: str
    is_running: bool


async def _list_markers(server: Server, stack: AgentStack) -> list[_RemoteMarker]:
    """List ``run_results/scheduled_run_*.json`` files via a single SSH call."""
    from app.services.ssh.commands import run_command

    remote_dir = f"{stack.stack_dir}/run_results"
    # Two-pattern ``ls`` — ``2>/dev/null || true`` swallows the
    # "no such file" stderr when the directory exists but has no
    # matches yet.
    cmd = (
        f"ls -1 {shlex.quote(remote_dir)}/{_FINAL_FILENAME_PREFIX}*.json "
        f"2>/dev/null || true"
    )
    result = await run_command(server, cmd, timeout=20.0, check=False)
    out: list[_RemoteMarker] = []
    for line in result.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        name = path.rsplit("/", 1)[-1]
        if name.endswith(_RUNNING_SUFFIX):
            out.append(_RemoteMarker(name=name, full_path=path, is_running=True))
        elif name.endswith(_FINAL_SUFFIX) and name.startswith(_FINAL_FILENAME_PREFIX):
            out.append(_RemoteMarker(name=name, full_path=path, is_running=False))
        # else: foreign file, skip
    # Final markers go before running markers so the UPSERT terminal-state
    # ones land first, and a still-present running marker for the SAME id
    # then no-ops because the row is already terminal.
    out.sort(key=lambda m: (m.is_running, m.name))
    return out


async def _fetch_marker(server: Server, path: str) -> Optional[dict]:
    """SFTP-read + parse one marker. ``None`` on any parse error."""
    from app.services.ssh.sftp import sftp_read_text

    try:
        body = await sftp_read_text(server, path)
    except SSHError as exc:
        logger.warning("scheduled_run marker read failed for %s: %s", path, exc)
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("scheduled_run marker not valid JSON: %s", path)
        return None
    if not isinstance(payload, dict):
        logger.warning("scheduled_run marker is not an object: %s", path)
        return None
    return payload


async def _delete_remote(server: Server, path: str) -> None:
    """Best-effort ``rm -f`` after a successful UPSERT."""
    from app.services.ssh.commands import run_command

    try:
        await run_command(server, f"rm -f {shlex.quote(path)}", timeout=10.0)
    except Exception:  # noqa: BLE001 — non-fatal; next tick will re-process
        logger.warning("failed to delete consumed marker %s", path, exc_info=True)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 strictly; return ``None`` on garbage or absence."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _upsert_run_from_marker(
    db: AsyncSession,
    *,
    stack: AgentStack,
    payload: dict,
    server: Optional[Server] = None,
) -> tuple[str, str, Optional[str]]:
    """Insert a new ``runs`` row from the marker, or update the existing.

    Returns ``(action, reason, consumed_log_path)`` where ``action`` is one
    of ``inserted`` / ``updated`` / ``skipped`` for the caller's counters and
    ``consumed_log_path`` is the remote ``.log.gz`` the archive step consumed
    (delete after commit), or ``None``.

    Idempotency: dedup on ``runs.id == scheduled_run_id``. If a row
    already exists in a terminal state and the marker is a "running"
    one, we skip (running marker arrived late / after the final).
    """
    if payload.get("schema_version") != _SUPPORTED_SCHEMA:
        return "skipped", f"schema {payload.get('schema_version')!r} unsupported", None
    job_name = payload.get("job_name")
    if job_name not in _ALLOWED_JOB_NAMES:
        return "skipped", f"job_name {job_name!r} not in enum", None
    try:
        run_uuid = UUID(payload["scheduled_run_id"])
    except (KeyError, ValueError, TypeError):
        return "skipped", "missing or malformed scheduled_run_id", None

    started_at = _parse_iso(payload.get("started_at")) or datetime.now(timezone.utc)
    finished_at = _parse_iso(payload.get("finished_at"))
    raw_status = payload.get("status") or "running"
    if raw_status not in ("running", "success", "failed", "killed"):
        return "skipped", f"status {raw_status!r} not in enum", None
    exit_code = payload.get("exit_code")
    if exit_code is not None:
        try:
            exit_code = int(exit_code)
        except (TypeError, ValueError):
            exit_code = None

    existing = await db.get(Run, run_uuid)
    if existing is None:
        # First sighting of this scheduled fire — INSERT a fresh row.
        # ``agent_id`` is the stack's owning agent (each stack belongs
        # to one agent — see services.stacks.find_or_create_stack).
        run = Run(
            id=run_uuid,
            stack_id=stack.id,
            agent_id=stack.agent_id,
            job_name=job_name,
            trigger="scheduled",
            started_at=started_at,
            finished_at=finished_at,
            status=raw_status,
            exit_code=exit_code,
            log_blob_ref=None,
            log_blob_sha256=None,
        )
        db.add(run)
        await db.flush()
        consumed_log = await _archive_log_if_final(
            run, payload, server=server, stack=stack
        )
        run_snapshot = {
            "id": str(run.id), "stack_id": str(run.stack_id),
            "agent_id": str(run.agent_id), "job_name": run.job_name,
            "trigger": run.trigger, "status": run.status,
            "exit_code": run.exit_code,
        }
        # Always emit a ``run.start`` audit row so the run-detail audit
        # trail records the start event even when we only ever observe
        # the final marker (common case with 30s polling against short
        # cache_warmup runs). The ``after_json`` snapshots the row at
        # logical start (running, no exit_code, no finished_at) so the
        # diff against the terminal audit below reads cleanly.
        start_snapshot = {
            **run_snapshot,
            "status": "running",
            "exit_code": None,
        }
        db.add(AuditLog(
            actor_user_id=None,
            action="run.start",
            target_type="run",
            target_id=str(run.id),
            before_json=None,
            after_json=start_snapshot,
            ts=datetime.now(timezone.utc),
        ))
        # If this insert ALSO carries a terminal state (one-tick case),
        # emit the matching run.complete / run.fail right after so the
        # audit reflects the actual landing state, not just the start.
        if raw_status != "running":
            db.add(AuditLog(
                actor_user_id=None,
                action="run.complete" if raw_status == "success" else "run.fail",
                target_type="run",
                target_id=str(run.id),
                before_json=start_snapshot,
                after_json=run_snapshot,
                ts=datetime.now(timezone.utc),
            ))
        return "inserted", raw_status, consumed_log

    # Existing row — only meaningful transition is running → terminal.
    if existing.status != "running":
        # Full-log retry: an earlier tick flipped the row terminal but the gz
        # fetch failed (tails were archived) and the marker was kept. Within
        # the retry window, re-attempt the fetch; on success the caller
        # deletes marker + gz. After the window, consume the marker (tails
        # stand; the orphan gz ages out via the bot's 7-day prune).
        if (
            raw_status != "running"
            and payload.get("log_file")
            and not str(existing.log_blob_ref or "").endswith(".gz")
        ):
            if _full_log_retry_open(payload):
                consumed_log = await _archive_log_if_final(
                    existing, payload, server=server, stack=stack
                )
                if consumed_log:
                    return "updated", "full-log archived on retry", consumed_log
                return "skipped", "full-log retry pending", None
            # "updated" (not "skipped") so the caller consumes the marker —
            # the row itself is unchanged; only the retry loop ends here.
            return "updated", "full-log retry window expired — keeping tails", None
        return "skipped", f"row already terminal ({existing.status})", None
    if raw_status == "running":
        return "skipped", "running marker for a row already at running", None

    before = {
        "id": str(existing.id), "status": existing.status,
        "exit_code": existing.exit_code, "finished_at":
            existing.finished_at.isoformat() if existing.finished_at else None,
    }
    existing.status = raw_status
    existing.exit_code = exit_code
    existing.finished_at = finished_at or datetime.now(timezone.utc)
    consumed_log = await _archive_log_if_final(
        existing, payload, server=server, stack=stack
    )
    db.add(AuditLog(
        actor_user_id=None,
        action="run.complete" if raw_status == "success" else "run.fail",
        target_type="run",
        target_id=str(existing.id),
        before_json=before,
        after_json={
            "id": str(existing.id), "status": existing.status,
            "exit_code": existing.exit_code,
            "finished_at": existing.finished_at.isoformat(),
        },
        ts=datetime.now(timezone.utc),
    ))
    return "updated", raw_status, consumed_log


async def _archive_log_if_final(
    run: Run,
    payload: dict,
    *,
    server: Optional[Server] = None,
    stack: Optional[AgentStack] = None,
) -> Optional[str]:
    """If the marker is final, archive the run's log.

    Preferred path: the marker names a ``log_file`` (the FULL combined
    output, gzip-compressed, written by the bot next to the marker). We
    fetch it over SFTP and store the gz bytes AS-FETCHED at
    ``RUN_LOGS_DIR/<run_id>.log.gz`` — complete log, ~10-20x less disk,
    zero re-compression. Returns the consumed remote path so the caller
    can delete it after commit.

    Fallback (old bot images, fetch/validation failure): the marker's 4 KB
    stdout/stderr tails are written to ``RUN_LOGS_DIR/<run_id>.log`` exactly
    as before; returns ``None`` so the caller keeps the marker + remote gz
    for a bounded retry on later ticks (see ``_full_log_retry_open``).
    """
    status = payload.get("status")
    if status not in ("success", "failed", "killed"):
        return None  # Not a final marker; nothing to archive yet.

    log_dir = Path(get_settings().run_logs_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = payload.get("log_file")
    if log_file and server is not None and stack is not None:
        # The filename comes from a remote-writable JSON — accept ONLY the
        # exact name the bot would emit for THIS run (no path traversal).
        expected = f"scheduled_run_{payload.get('scheduled_run_id')}.log.gz"
        if log_file != expected:
            logger.warning(
                "scheduled_run %s: marker log_file %r != expected %r — ignoring",
                run.id, log_file, expected,
            )
        else:
            from app.services.ssh.sftp import sftp_read_bytes

            remote_path = f"{stack.stack_dir}/run_results/{expected}"
            try:
                data = await sftp_read_bytes(
                    server, remote_path, max_bytes=_MAX_GZ_BYTES
                )
                if not _gzip_decompresses_within(data, _MAX_LOG_BYTES):
                    raise ValueError("gz payload invalid or exceeds size cap")
                log_path = log_dir / f"{run.id}.log.gz"
                log_path.write_bytes(data)
                try:
                    log_path.chmod(0o600)
                except OSError:
                    pass
                run.log_blob_ref = str(log_path)
                run.log_blob_sha256 = hashlib.sha256(data).hexdigest()
                return remote_path
            except (SSHError, ValueError, OSError) as exc:
                logger.warning(
                    "scheduled_run %s: full-log fetch failed (%s) — "
                    "falling back to marker tails", run.id, exc,
                )

    stdout = (payload.get("stdout_tail") or "").encode("utf-8", errors="replace")
    stderr = (payload.get("stderr_tail") or "").encode("utf-8", errors="replace")
    sep = b"\n--- stderr ---\n" if stderr else b""
    blob = stdout + sep + stderr
    log_path = log_dir / f"{run.id}.log"
    log_path.write_bytes(blob)
    try:
        log_path.chmod(0o600)
    except OSError:
        pass
    run.log_blob_ref = str(log_path)
    run.log_blob_sha256 = hashlib.sha256(blob).hexdigest()
    return None


async def ingest_stack_once(
    db: AsyncSession,
    *,
    stack_id: UUID,
) -> IngestResult:
    """Process every available scheduled-run marker for one stack.

    Holds a transaction-scoped advisory lock so concurrent worker ticks
    don't double-process the same files. Successful UPSERTs delete the
    remote file; failures are recorded in ``result.errors`` and the
    file is left for the next tick.

    Never raises: unexpected exceptions are caught and stuffed into
    ``result.errors`` so a per-stack failure can't wedge the caller's
    loop over every stack.
    """
    result = IngestResult(stack_id=stack_id)
    lock_key = hash_lock_key("scheduled_run_ingest", str(stack_id))
    try:
        gate = await db.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_key}
        )
        if not gate.scalar():
            # rollback() releases the advisory lock (it's transaction-
            # scoped) and the connection back to the pool. Without this
            # the lock sits with us until the caller's `async with` exits,
            # which on a busy fleet can be many seconds longer than needed.
            await db.rollback()
            result.errors.append("another tick is in flight for this stack")
            return result

        stack = await db.get(AgentStack, stack_id)
        if stack is None:
            await db.rollback()
            result.errors.append(f"stack {stack_id} not found")
            return result
        server = await db.get(Server, stack.server_id)
        if server is None:
            await db.rollback()
            result.errors.append(f"server {stack.server_id} not found")
            return result

        try:
            markers = await _list_markers(server, stack)
        except SSHError as exc:
            await db.rollback()
            result.errors.append(f"ssh list failed: {exc}")
            return result
        result.files_seen = len(markers)

        deletes: list[str] = []
        for m in markers:
            payload = await _fetch_marker(server, m.full_path)
            if payload is None:
                # Couldn't even read the file — leave it for the next
                # tick rather than risk losing a marker that might be
                # parseable on retry.
                result.rows_skipped += 1
                continue
            # Wrap each UPSERT in a savepoint. If `_archive_log_if_final`
            # (file I/O — disk full, perm denied) raises mid-marker,
            # SQLAlchemy rolls back to BEFORE the dirty Run/AuditLog
            # additions for THIS marker only. Without the savepoint,
            # the eventual `await db.commit()` below would persist a
            # Run row without its log archive / matching audit, leaving
            # an inconsistent partial in the database.
            action: str = "skipped"
            consumed_log: Optional[str] = None
            try:
                async with db.begin_nested():
                    action, _, consumed_log = await _upsert_run_from_marker(
                        db, stack=stack, payload=payload, server=server
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("upsert failed for %s", m.name)
                result.errors.append(f"{m.name}: {exc}")
                continue
            if action == "inserted":
                result.rows_inserted += 1
            elif action == "updated":
                result.rows_updated += 1
            else:
                result.rows_skipped += 1
            # Only schedule the remote delete when the marker actually
            # persisted into a row. Skipped markers — schema-version
            # mismatch, unknown job_name, malformed UUID, terminal-row
            # collision — get LEFT IN PLACE so a future ingestor that
            # understands the new schema (or a fixed bot) can still
            # process them. Deleting on skip would turn a temporary
            # version skew into permanent data loss.
            if action in ("inserted", "updated"):
                is_final = payload.get("status") in ("success", "failed", "killed")
                if (
                    is_final
                    and payload.get("log_file")
                    and consumed_log is None
                    and _full_log_retry_open(payload)
                ):
                    # Tails fallback with the retry window still open: KEEP
                    # the marker (and the remote gz) so the next tick can
                    # retry the full-log fetch against the now-terminal row.
                    pass
                else:
                    deletes.append(m.full_path)
                # The consumed full-log gz is deleted alongside its marker
                # (only set when the archive actually stored it).
                if consumed_log:
                    deletes.append(consumed_log)

        await db.commit()
        for path in deletes:
            await _delete_remote(server, path)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("scheduled_run_ingest unexpected failure for %s", stack_id)
        result.errors.append(f"unexpected: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return result


__all__ = ["IngestResult", "ingest_stack_once"]
