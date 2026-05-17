"""Scheduler-job CRUD orchestration (Phase 5).

The trading bot reads ``scheduler_config.json`` once a second
(:mod:`SellerMarket.scheduler`) and fires whichever job's ``time`` matches
the current wall-clock minute. Each (date, name, time) tuple acts as a
dedupe key — meaning a time change on a job that has already fired today
produces a NEW key and the job re-fires. The mgmt UI surfaces a warning
banner so the operator knows that's about to happen; see
:func:`will_refire_today`.

This module is the seam between the HTTP router and:

* :mod:`app.models.scheduler` for the ``scheduler_jobs`` row.
* :mod:`app.models.audit` for write-side audit logging.
* :mod:`app.schemas.scheduler` for input shape + the command whitelist.

We deliberately do NOT touch SSH or the rendering layer here. A successful
upsert just persists the row; the route caller is responsible for kicking
off the re-render / SFTP push of ``scheduler_config.json`` (in the same way
the customer service leaves the ``config.ini`` push to the route).
"""

from __future__ import annotations

import logging
from datetime import datetime, time as time_type
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.scheduler import SchedulerJob
from app.schemas.scheduler import (
    ALLOWED_COMMANDS,
    SchedulerJobUpsert,
    WillReFireToday,
    is_command_allowed,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OptimisticLockError(Exception):
    """Raised when the caller's ``version`` doesn't match the row's.

    The router catches this and returns HTTP 409 with a "reload and retry"
    message. Typed (rather than a generic ``ValueError``) so the router can
    distinguish it from the whitelist / unknown-job rejections, which also
    bubble up as ``ValueError``.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_time(time_str: str) -> time_type:
    """Parse ``HH:MM:SS`` into a :class:`datetime.time`.

    The schema validator already enforced the wire format, so this is just
    a strict re-parse for the ORM. We split manually rather than use
    :meth:`time.fromisoformat` because the latter happily accepts
    fractional seconds and timezone suffixes — both of which would silently
    drift away from the bot's scheduler key format.
    """
    hh, mm, ss = time_str.split(":")
    return time_type(int(hh), int(mm), int(ss))


def _public_snapshot(job: SchedulerJob) -> dict:
    """Audit-safe dict of a :class:`SchedulerJob` row.

    The row carries no secret material; we include the full set of editable
    fields so an audit reader can reconstruct exactly what changed without
    cross-referencing other tables. ``time`` is serialised as its
    ``HH:MM:SS`` string form so the JSONB value is human-readable in
    ``audit_log`` dumps.
    """
    return {
        "id": str(job.id),
        "stack_id": str(job.stack_id),
        "name": job.name,
        "time": job.time.isoformat(timespec="seconds")
        if job.time is not None
        else None,
        "enabled": job.enabled,
        "command": job.command,
        "version": job.version,
    }


async def _write_audit(
    db: AsyncSession,
    *,
    actor_id: Optional[UUID],
    action: str,
    target_id: UUID,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
) -> None:
    """Insert a single ``audit_log`` row for a scheduler-job mutation.

    ``target_type`` is always ``"scheduler_job"`` here. Mirrors the pattern
    in :mod:`app.services.customers` and :mod:`app.services.stacks` so the
    audit-log subscribers can rely on a consistent shape across services.
    """
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action=action,
            target_type="scheduler_job",
            target_id=str(target_id),
            before_json=before,
            after_json=after,
        )
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_jobs(
    db: AsyncSession, *, stack_id: UUID
) -> list[SchedulerJob]:
    """Return both jobs for a stack, ordered (``cache_warmup``, ``run_trading``).

    The UI expects a stable order so a refresh doesn't reshuffle the form
    rows under the operator's cursor. We sort by ``name DESC`` because the
    string ``"cache_warmup"`` sorts BEFORE ``"run_trading"`` alphabetically
    — wait, ascending also puts ``cache_warmup`` first because ``c < r``.
    We use ascending name order for clarity.

    A freshly-provisioned stack may have zero or one job (the operator
    hasn't filled in both forms yet), so the returned list is 0-2 elements.
    """
    stmt = (
        select(SchedulerJob)
        .where(SchedulerJob.stack_id == stack_id)
        .order_by(SchedulerJob.name.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_job(
    db: AsyncSession, stack_id: UUID, name: str
) -> Optional[SchedulerJob]:
    """Look up one job by ``(stack_id, name)`` — the natural composite key.

    Returns ``None`` if the row hasn't been created yet. The composite
    UNIQUE on ``(stack_id, name)`` guarantees at most one match, so we use
    ``scalar_one_or_none``.
    """
    stmt = select(SchedulerJob).where(
        SchedulerJob.stack_id == stack_id,
        SchedulerJob.name == name,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


async def upsert_job(
    db: AsyncSession,
    stack_id: UUID,
    name: str,
    data: SchedulerJobUpsert,
    actor_id: UUID,
) -> SchedulerJob:
    """Create-or-update one scheduler job for a stack.

    Workflow:

    1. Resolve the command. If ``data.command`` is ``None`` we fall back to
       the canonical default for ``name`` from :data:`ALLOWED_COMMANDS`.
       If the resolved command isn't in the whitelist we raise
       :class:`ValueError` — the route translates that to a 422.
    2. Parse ``data.time`` (HH:MM:SS) into a :class:`datetime.time`.
    3. Look up the existing row by ``(stack_id, name)``.
    4. If a row exists:
        - Check ``data.version`` against the row's; mismatch raises
          :class:`OptimisticLockError`.
        - Update fields and bump version.
    5. If no row exists, insert with ``version=1`` (regardless of what the
       caller passed — first-time creates use ``version=0`` in the form,
       which is just a "this is a create" sentinel).
    6. Audit ``scheduler.update`` with before/after snapshots.
    7. If the insert hits :class:`IntegrityError` (someone won the
       UNIQUE race between our SELECT and our INSERT), retry the SELECT
       and treat it as an update. If the retry still fails we let the
       error propagate — the route returns 500 and the operator can retry.

    Raises:
        ValueError: unknown ``name`` (not in :data:`ALLOWED_COMMANDS`) or
            command outside the whitelist.
        OptimisticLockError: ``data.version`` doesn't match the row's
            current ``version``.
    """
    # Step 0: validate the job name. The schema's ``Literal`` already
    # catches this at the HTTP boundary, but a service-layer caller (e.g. a
    # future bulk-import script) might bypass the route. Mirror the same
    # message the schema raises so the error surface is consistent.
    if name not in ALLOWED_COMMANDS:
        raise ValueError(
            f"unknown scheduler job name: {name!r} "
            f"(expected one of {tuple(ALLOWED_COMMANDS)})"
        )

    # Step 1: resolve command. The canonical default is the first entry in
    # the per-name whitelist tuple.
    command = data.command if data.command is not None else ALLOWED_COMMANDS[name][0]
    if not is_command_allowed(name, command):
        raise ValueError(
            f"command not in whitelist for job {name!r}: {command!r}"
        )

    # Step 2: parse the time string into a ``datetime.time``. The schema
    # already enforced the wire format.
    new_time = _parse_time(data.time)

    # Step 3: existing row?
    existing = await get_job(db, stack_id, name)

    if existing is not None:
        # Step 4: update path — version check first.
        if existing.version != data.version:
            raise OptimisticLockError(
                f"version mismatch: row has {existing.version}, "
                f"caller had {data.version}"
            )

        before = _public_snapshot(existing)
        existing.time = new_time
        existing.enabled = data.enabled
        existing.command = command
        existing.version += 1
        await _write_audit(
            db,
            actor_id=actor_id,
            action="scheduler.update",
            target_id=existing.id,
            before=before,
            after=_public_snapshot(existing),
        )
        await db.commit()
        await db.refresh(existing)
        return existing

    # Step 5: insert path. ``version=1`` regardless of what the caller
    # passed; the form sends ``version=0`` as the "this is a create" signal.
    job = SchedulerJob(
        stack_id=stack_id,
        name=name,
        time=new_time,
        enabled=data.enabled,
        command=command,
        version=1,
    )
    db.add(job)

    try:
        # Flush to surface the UNIQUE-race :class:`IntegrityError` before we
        # write the audit log row (so we don't leave an orphan audit entry
        # pointing at an id the DB rejected).
        await db.flush()
    except IntegrityError:
        # Step 7: someone else won the race between our SELECT and our
        # INSERT. Roll the failed insert back, re-SELECT, and treat as an
        # update. If the second SELECT comes up empty, something stranger
        # is going on (e.g. a non-UNIQUE constraint fired) — re-raise so
        # the route turns it into a 500 instead of looping forever.
        await db.rollback()
        existing = await get_job(db, stack_id, name)
        if existing is None:
            logger.exception(
                "upsert_job: IntegrityError on insert and re-SELECT "
                "found no row for stack=%s name=%s",
                stack_id,
                name,
            )
            raise
        # Recurse with the caller's payload but using the now-known
        # version. This keeps the optimistic-lock semantics: if the row
        # the racing INSERT wrote already moved past version 1 by the time
        # we retry, the operator will get an OptimisticLockError and a
        # chance to reload — which is correct.
        if existing.version != data.version:
            raise OptimisticLockError(
                f"version mismatch after race: row has {existing.version}, "
                f"caller had {data.version}"
            )
        before = _public_snapshot(existing)
        existing.time = new_time
        existing.enabled = data.enabled
        existing.command = command
        existing.version += 1
        await _write_audit(
            db,
            actor_id=actor_id,
            action="scheduler.update",
            target_id=existing.id,
            before=before,
            after=_public_snapshot(existing),
        )
        await db.commit()
        await db.refresh(existing)
        return existing

    await _write_audit(
        db,
        actor_id=actor_id,
        action="scheduler.update",
        target_id=job.id,
        before=None,
        after=_public_snapshot(job),
    )
    await db.commit()
    await db.refresh(job)
    return job


# ---------------------------------------------------------------------------
# Re-fire heuristic
# ---------------------------------------------------------------------------


async def will_refire_today(
    db: AsyncSession,
    stack_id: UUID,
    name: str,
    new_time_str: str,
    *,
    now_local: Optional[datetime] = None,
) -> WillReFireToday:
    """Best-effort: will changing this job's time cause it to re-fire today?

    The bot's scheduler keys executions on ``(name, date, time_str)`` and
    re-reads the JSON every second. So:

    * If the OLD time is still in the future today, the bot hasn't fired
      yet today — no risk of a duplicate. Return ``False``.
    * If the OLD time has already passed today AND the NEW time is also in
      the past today, the bot has already fired and won't fire again
      today — return ``False``.
    * If the OLD time has passed AND the NEW time is in the future today,
      the bot will produce a fresh dedupe key (because ``time_str`` is
      different from the one it fired earlier) and re-fire — return ``True``.

    We approximate "today" using the server's local clock for this phase.
    The trading server runs in ``Asia/Tehran``; a future revision can wire
    per-server TZ awareness through here without changing the call site.

    The ``now_local`` parameter exists so tests can inject a fixed wall
    clock. The route caller passes :func:`datetime.now` with no arguments.

    Returns a :class:`WillReFireToday` with both the flag and a human-
    readable reason so the UI can show *why* it's warning the operator.
    """
    if now_local is None:
        now_local = datetime.now()

    # If the row doesn't exist yet, there's nothing to compare against —
    # this is a fresh create, not a "change". Be conservative and warn only
    # if the new time is still in the future today; otherwise no fire.
    existing = await get_job(db, stack_id, name)
    new_t = _parse_time(new_time_str)
    now_t = now_local.time()

    if existing is None:
        # A brand-new job inserted with a future time today will fire today;
        # that's not a "re-fire", but the UI banner copy is general enough
        # that this branch is best handled by returning False (no risk of
        # double-run) and letting the route show a different message.
        return WillReFireToday(
            will_refire=False,
            reason="no existing job for this (stack, name); first-time create",
        )

    old_t = existing.time

    # Case 1: old time still in the future today → bot hasn't fired today.
    if old_t > now_t:
        return WillReFireToday(
            will_refire=False,
            reason=(
                f"current time {old_t.isoformat(timespec='seconds')} is "
                f"still in the future today; no fire has occurred yet"
            ),
        )

    # Old time is now in the past (or equal to current minute). The bot
    # MAY have fired already today. Whether the new time triggers another
    # fire depends on whether the new time is still ahead of ``now``.
    if new_t > now_t:
        return WillReFireToday(
            will_refire=True,
            reason=(
                f"current time {old_t.isoformat(timespec='seconds')} has "
                f"already passed today; new time "
                f"{new_t.isoformat(timespec='seconds')} is in the future "
                f"and produces a fresh dedupe key, so the bot will re-fire"
            ),
        )

    # New time is also in the past — even though the dedupe key changes,
    # the bot only fires when the wall clock matches ``time``, and that
    # moment has gone by.
    return WillReFireToday(
        will_refire=False,
        reason=(
            f"both old ({old_t.isoformat(timespec='seconds')}) and new "
            f"({new_t.isoformat(timespec='seconds')}) times are already "
            f"in the past today; the bot will not fire again until tomorrow"
        ),
    )


__all__ = [
    "OptimisticLockError",
    "get_job",
    "list_jobs",
    "upsert_job",
    "will_refire_today",
]
