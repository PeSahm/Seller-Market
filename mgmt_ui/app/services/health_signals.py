"""Scan a stack's ``trading_bot.log`` tail for anomaly patterns (Phase 8).

The in-container trading bot writes a single log file per stack at
``<stack_dir>/trading_bot.log``. On a fixed cadence (handled by the worker
in 8.D, not here) we tail the last 500 lines of that file over SSH and
extract anomalies — broker rate limits, captcha failures, broker auth
failures, OCR outages, etc. — using a regex catalogue defined as
:data:`_PATTERNS` near the top of this module.

Each detected anomaly is upserted into ``health_signals`` with a 60-minute
dedup window per ``(stack_id, kind)``: if a matching row already exists
within that window and hasn't been ack'd, we bump its ``ts`` to "now"
instead of inserting a fresh row. This keeps the table small under chronic
conditions (broker rate-limited for hours) while still surfacing the
"still happening" signal to the operator.

We deliberately do NOT keep a cursor here (unlike the trade ingestor): if
the scanner misses a tick or the worker restarts, the next tick re-tails
the same window and dedup absorbs the duplication. The cost of re-matching
500 lines is negligible and the alternative (per-stack cursor with line
offsets) is fragile against log rotation.

We don't import :mod:`paramiko` at module load — SSH helpers are imported
lazily inside :func:`scan_stack_once` so :data:`_PATTERNS` can be
inspected without dragging the SSH stack in (which matters for tooling
and tests).

Errors are caught and surfaced via :class:`HealthScanResult.error` rather
than raised, because the caller is a loop over many stacks and we don't
want one broken stack (bad ssh creds, missing logfile after a
provisioning failure) to wedge the others.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import hash_lock_key
from app.models.audit import AuditLog
from app.models.health import HealthSignal
from app.models.servers import Server
from app.models.stacks import AgentStack
from app.schemas.health import HealthScanResult

# NOTE: ``app.services.ssh.exceptions`` is deliberately NOT imported at module
# load — ``app/services/ssh/__init__.py`` re-exports symbols that pull in
# paramiko (and transitively pynacl). The acceptance criterion for this
# module is "``from app.services.health_signals import _PATTERNS, match_line``
# works with no env vars set", which implies no SSH-stack drag-in. We
# import :class:`SSHError` lazily inside :func:`scan_stack_once` instead.

logger = logging.getLogger(__name__)


# Dedup window — within this many minutes per (stack_id, kind), an
# unacked existing row is bumped instead of a new row being inserted.
# 60 minutes matches the operator's "chronic vs. recurring" intuition:
# the same broker rate-limit can fire many times an hour without us
# wanting fifty separate rows for it.
_DEDUP_WINDOW = timedelta(minutes=60)

# How many bytes of the matching log line to persist on the row. A bot
# stack trace pasted into a single line can easily be 50+ KB; we keep
# the *tail* so we get the actual error message and surrounding context
# rather than the start of a useless stack header.
_RAW_TRUNC_BYTES = 2000

# How many trailing lines of the trading_bot.log we tail per tick. The
# log volume runs ~1 line/sec during active hours, so 500 lines covers
# ~8 minutes of activity — comfortably more than the worker's tick
# interval.
_TAIL_LINES = 500


@dataclass(frozen=True)
class CompiledPattern:
    """One entry in the anomaly catalogue.

    ``regex`` is pre-compiled with :data:`re.IGNORECASE` so callers don't
    have to re-think case sensitivity per pattern; broker log lines mix
    cases freely (``"401 Unauthorized"`` vs ``"unauthorized"``).
    """

    regex: re.Pattern[str]
    kind: str
    severity: str
    message: str


def _c(regex: str, kind: str, severity: str, message: str) -> CompiledPattern:
    """Compile one catalogue entry. Tiny helper to keep the list dense."""
    return CompiledPattern(
        regex=re.compile(regex, re.IGNORECASE),
        kind=kind,
        severity=severity,
        message=message,
    )


# Order matters only insofar as the FIRST matching pattern wins per line
# (we stop scanning on the first hit). Where two patterns could plausibly
# match the same line, the more specific one should appear first.
#
# Persian "موجودی کافی" / "عدم موجودی" is matched via the same alternation
# as the English phrases — operator-facing brokers in Iran emit either,
# depending on the API surface in use.
_PATTERNS: list[CompiledPattern] = [
    _c(
        r"\b(429|too many requests)\b",
        "broker_rate_limit",
        "warning",
        "Broker returned 429",
    ),
    _c(
        r"(captcha.*(failed?|incorrect)|wrong\s*captcha|invalid\s*captcha)",
        "captcha_fail",
        "warning",
        "Captcha decode failed",
    ),
    _c(
        r"\b(401|unauthori[sz]ed|auth(entication)?\s+failed?|login\s+failed?)\b",
        "auth_failed",
        "error",
        "Broker auth failed (401)",
    ),
    _c(
        r"(insufficient\s+(buying\s+power|funds|balance)|"
        r"موجودی\s*کافی|عدم\s*موجودی)",
        "insufficient_funds",
        "info",
        "Insufficient funds at broker",
    ),
    _c(
        r"(ocr|easyocr).*(unavailable|unreachable|connection\s*(refused|reset))",
        "ocr_down",
        "critical",
        "OCR service unreachable",
    ),
    _c(
        r"(connection\s+(refused|reset)|connectionerror)"
        r".*(broker|ephoenix|ibtrader)",
        "broker_unreachable",
        "error",
        "Broker connection refused",
    ),
    _c(
        r"(maxretries.*broker|broker.*timed?\s*out)",
        "broker_timeout",
        "error",
        "Broker timed out",
    ),
]


def match_line(line: str) -> Optional[CompiledPattern]:
    """Return the first :class:`CompiledPattern` matching ``line``, or ``None``.

    Empty / whitespace-only lines short-circuit to ``None`` so the caller
    doesn't have to special-case them. Matching is case-insensitive
    because every pattern is compiled with :data:`re.IGNORECASE`.
    """
    if not line or not line.strip():
        return None
    for pat in _PATTERNS:
        if pat.regex.search(line):
            return pat
    return None


def _message_hash(message: str) -> str:
    """Short, stable, non-security hash of a message — for log breadcrumbs.

    Truncated SHA-256 because Python's built-in ``hash()`` is randomised
    per process and useless across worker restarts. 16 hex chars (64 bits)
    is plenty for "different lines should produce different hashes" — we
    only use it in debug logging, not as a uniqueness key.
    """
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]


async def _upsert_signal(
    db: AsyncSession,
    *,
    stack_id: UUID,
    kind: str,
    severity: str,
    message: str,
    raw: str,
) -> tuple[str, HealthSignal]:
    """Insert one ``health_signals`` row, or bump an existing one's ``ts``.

    Dedup rules:

    * Look for an existing row with same ``(stack_id, kind)`` whose
      ``ack_at IS NULL`` and ``ts > now - 60m``.
    * If one exists: update its ``ts`` to "now" and return
      ``("bumped", row)``. The operator sees a single still-active row
      with a fresh timestamp rather than a flood of duplicates.
    * If none exists OR the most recent one has been ack'd: insert a
      fresh row and return ``("inserted", row)``. The "ack'd row doesn't
      satisfy dedup" rule is deliberate — once an operator has
      acknowledged a signal, the next occurrence is "this is happening
      AGAIN after you said you handled it" and deserves its own row.

    The advisory lock in :func:`scan_stack_once` is at the tick level,
    so within one tick we don't worry about another concurrent insert
    for the same stack. We do NOT commit here — the caller commits once
    at the end of the tick so the whole batch is atomic.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - _DEDUP_WINDOW
    stmt = (
        select(HealthSignal)
        .where(
            HealthSignal.stack_id == stack_id,
            HealthSignal.kind == kind,
            HealthSignal.ack_at.is_(None),
            HealthSignal.ts > cutoff,
        )
        .order_by(desc(HealthSignal.ts))
        .limit(1)
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing is not None:
        existing.ts = now
        logger.debug(
            "health_signals: bumped stack=%s kind=%s hash=%s",
            stack_id,
            kind,
            _message_hash(message),
        )
        return ("bumped", existing)

    # Truncate raw from the END so we keep the tail (where the actual
    # error context usually lives) rather than the start of a verbose
    # stack-trace header. Encoding-safe because ``raw[-N:]`` slices on
    # code points; the storage column is TEXT so a 2000-char cap is
    # comfortably under any reasonable line.
    raw_trimmed = raw[-_RAW_TRUNC_BYTES:] if raw else None
    row = HealthSignal(
        stack_id=stack_id,
        kind=kind,
        severity=severity,
        message=message,
        raw=raw_trimmed,
        ts=now,
    )
    db.add(row)
    await db.flush()
    logger.info(
        "health_signals: inserted stack=%s kind=%s severity=%s hash=%s",
        stack_id,
        kind,
        severity,
        _message_hash(message),
    )
    return ("inserted", row)


async def scan_stack_once(
    db: AsyncSession,
    *,
    stack_id: UUID,
) -> HealthScanResult:
    """One scanner tick for one stack. Acquires advisory lock — callers don't.

    Never raises: unexpected exceptions are caught and stuffed into the
    returned :attr:`HealthScanResult.error` field so a per-stack failure
    can't wedge the caller's loop over every stack.

    Transactional model: every state change inside the lock (signal
    inserts, ts bumps) lives in the same transaction. We commit at the
    bottom on the happy path or roll back in the broad ``except``. The
    advisory lock is transaction-scoped, so a rollback releases it
    automatically.

    Missing logfile (fresh stack that's never run) is NOT an error — we
    return cleanly with ``lines_scanned=0``. SSH failures (creds, network)
    are surfaced via ``error`` so the worker can keep ticking on the
    next stack.
    """
    summary = HealthScanResult(stack_id=stack_id)
    lock_key = hash_lock_key("health", str(stack_id))
    try:
        gate = await db.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_key}
        )
        if not gate.scalar():
            summary.error = "lock busy"
            return summary

        stack = await db.get(AgentStack, stack_id)
        if stack is None:
            raise LookupError(f"stack {stack_id} not found")
        server = await db.get(Server, stack.server_id)
        if server is None:
            raise LookupError(f"server {stack.server_id} not found")

        # Lazy import keeps paramiko out of the module-load path so
        # ``from app.services.health_signals import _PATTERNS`` works
        # in tooling that doesn't have the SSH stack configured.
        from app.services.ssh.commands import run_command
        from app.services.ssh.exceptions import SSHError

        log_path = f"{stack.stack_dir}/trading_bot.log"
        # ``test -f`` short-circuits the tail when the file doesn't exist
        # yet (fresh stack). The ``|| echo __MISSING__`` sentinel lets us
        # distinguish "tail returned no lines" (file exists but empty,
        # which is a legitimate 0-line tick) from "no file at all".
        tail_cmd = (
            f"if [ -f {shlex.quote(log_path)} ]; then "
            f"tail -n {_TAIL_LINES} {shlex.quote(log_path)}; "
            f"else echo __MISSING__; fi"
        )
        try:
            result = await run_command(server, tail_cmd, timeout=30.0, check=False)
        except SSHError as exc:
            summary.error = f"ssh failed: {exc}"
            return summary

        stdout = result.stdout or ""
        # First-line MISSING sentinel means the bot hasn't created the
        # logfile yet — a fresh-provisioning state, not an error.
        if stdout.strip() == "__MISSING__":
            return summary

        lines = stdout.splitlines()
        summary.lines_scanned = len(lines)
        for line in lines:
            pat = match_line(line)
            if pat is None:
                continue
            action, _row = await _upsert_signal(
                db,
                stack_id=stack_id,
                kind=pat.kind,
                severity=pat.severity,
                message=pat.message,
                raw=line,
            )
            if action == "inserted":
                summary.signals_inserted += 1
            elif action == "bumped":
                summary.signals_bumped += 1

        await db.commit()
        return summary

    except LookupError as exc:
        summary.error = str(exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.exception("health_signals: unexpected error stack=%s", stack_id)
        summary.error = f"unexpected: {exc}"
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            # Already in an error path; suppressing a rollback failure
            # keeps the original cause as the surfaced error.
            pass
        return summary


async def ack_signal(
    db: AsyncSession,
    *,
    signal_id: UUID,
    actor_id: UUID,
) -> Optional[HealthSignal]:
    """Acknowledge a signal. Returns the row, or ``None`` if missing / already acked.

    The "already acked" no-op is deliberate: in a multi-operator setup
    two people may click "ack" near-simultaneously and we want the second
    click to be benign rather than overwriting the first ack's
    ``ack_by`` / ``ack_at`` (which would lose the audit trail of who
    actually triaged it first).

    Commits immediately so the ack is visible to other transactions
    before any UI redirect.
    """
    row = await db.get(HealthSignal, signal_id)
    if row is None:
        return None
    if row.ack_at is not None:
        return None
    now = datetime.now(timezone.utc)
    row.ack_by = actor_id
    row.ack_at = now
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action="health.ack",
            target_type="health_signal",
            target_id=str(signal_id),
            before_json=None,
            after_json={
                "kind": row.kind,
                "severity": row.severity,
                "stack_id": str(row.stack_id) if row.stack_id else None,
            },
            ts=now,
        )
    )
    await db.commit()
    await db.refresh(row)
    return row


async def list_signals(
    db: AsyncSession,
    *,
    stack_id: Optional[UUID] = None,
    kind: Optional[str] = None,
    severity: Optional[str] = None,
    acked: Optional[bool] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 200,
) -> list[HealthSignal]:
    """Filter signals on common dimensions; newest-first.

    Each filter is optional. ``acked=True`` requires ``ack_at IS NOT NULL``,
    ``acked=False`` requires ``ack_at IS NULL``, ``acked=None`` returns
    both. ``limit`` defaults to 200 to match the UI's table page; callers
    paginating manually can override.

    No tenant scoping happens here — the router layer is responsible for
    constraining ``stack_id`` to stacks the caller owns. Agents see only
    their own stacks; admins see everything.
    """
    stmt = (
        select(HealthSignal)
        .order_by(desc(HealthSignal.ts))
        .limit(limit)
    )
    if stack_id is not None:
        stmt = stmt.where(HealthSignal.stack_id == stack_id)
    if kind is not None:
        stmt = stmt.where(HealthSignal.kind == kind)
    if severity is not None:
        stmt = stmt.where(HealthSignal.severity == severity)
    if acked is True:
        stmt = stmt.where(HealthSignal.ack_at.is_not(None))
    elif acked is False:
        stmt = stmt.where(HealthSignal.ack_at.is_(None))
    if since is not None:
        stmt = stmt.where(HealthSignal.ts >= since)
    if until is not None:
        stmt = stmt.where(HealthSignal.ts <= until)
    result = await db.execute(stmt)
    return list(result.scalars().all())


__all__ = [
    "CompiledPattern",
    "HealthScanResult",
    "_PATTERNS",
    "match_line",
    "scan_stack_once",
    "ack_signal",
    "list_signals",
]
