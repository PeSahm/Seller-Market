"""Snapshot per-job ``enabled`` state, disable both jobs, restore on exit.

Used by manual-run flows so the in-container scheduler doesn't fire the
same job while the operator is running it manually — which would
duplicate-order at the broker (run_trading) or stomp on the warm
session cache mid-load (cache_warmup).

The snapshot is held in memory (and reflected in the audit log via the
surrounding service calls) inside the context manager. If the mgmt
process dies mid-run, the in-memory restore is lost; the next admin
save on the scheduler editor re-asserts the canonical state from the
DB. For long runs we accept that as a small window of degradation —
the alternative (persisting the snapshot in a side table) added more
moving parts than the failure mode warranted.

Design notes
------------
* The pusher is passed in as a parameter rather than imported at module
  top so unit tests can substitute a no-op without monkey-patching
  ``app.services.stacks``. The production caller passes
  :func:`app.services.stacks.push_scheduler_config_for_stack`.
* We skip the disable/push entirely when no job is currently enabled —
  there's nothing to suppress and a redundant SFTP write would still
  acquire the per-server compose lock and slow other ops.
* The restore branch in the ``finally`` does NOT re-raise on push
  failure: by the time we're restoring we may already be unwinding from
  an exception in the with-block, and masking that with a push error
  would lose the original cause. We log + drop and rely on the next
  admin save to re-assert state.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler import SchedulerJob

logger = logging.getLogger(__name__)


@asynccontextmanager
async def disable_scheduler_for_stack(
    db: AsyncSession,
    *,
    stack_id: UUID,
    pusher,
    actor_id: Optional[UUID],
) -> AsyncIterator[dict[str, bool]]:
    """Snapshot each job's ``enabled`` flag, disable all, restore on exit.

    Args:
        db: Session used for both the disable and restore writes. The
            same session is reused on the way out so the restore happens
            on the connection the caller already has.
        stack_id: Target stack — both jobs (if present) get toggled.
        pusher: An ``async (db, stack_id, *, actor_id) -> Any`` callable.
            In production this is
            :func:`app.services.stacks.push_scheduler_config_for_stack`;
            tests pass a no-op stub.
        actor_id: User performing the surrounding run. Threaded through
            to the pusher so the audit log on the push reflects the
            human operator rather than a service principal.

    Yields:
        A ``{job_name: enabled_before}`` snapshot so the caller can log
        or echo what was suppressed.

    Notes:
        If the initial disable push raises, we attempt to restore the DB
        rows back to their snapshot state before re-raising — otherwise
        the DB would show ``enabled=False`` while the on-server
        ``scheduler_config.json`` is whatever the failed push left.
    """
    rows = await db.execute(
        select(SchedulerJob).where(SchedulerJob.stack_id == stack_id)
    )
    jobs = list(rows.scalars().all())
    snapshot: dict[str, bool] = {j.name: j.enabled for j in jobs}

    had_enabled = any(snapshot.values())

    if had_enabled:
        # Bulk-disable everything for the stack in one statement, then
        # push the rendered config so the bot picks it up within ~1 s.
        await db.execute(
            update(SchedulerJob)
            .where(SchedulerJob.stack_id == stack_id)
            .values(enabled=False)
        )
        await db.commit()
        try:
            await pusher(db, stack_id, actor_id=actor_id)
        except Exception:
            # The disable write committed but the push failed — restore
            # the DB so it matches the (still-old) on-server file, then
            # re-raise so the caller knows the disable didn't take.
            logger.exception(
                "scheduler disable push failed for stack=%s; restoring DB state",
                stack_id,
            )
            for j in jobs:
                await db.execute(
                    update(SchedulerJob)
                    .where(SchedulerJob.id == j.id)
                    .values(enabled=snapshot[j.name])
                )
            await db.commit()
            raise

    try:
        yield snapshot
    finally:
        # ALWAYS restore — even on exception inside the with-block. We
        # only need to do work if we actually disabled something on the
        # way in; otherwise the DB is unchanged and there's nothing to
        # put back.
        if had_enabled:
            for j in jobs:
                await db.execute(
                    update(SchedulerJob)
                    .where(SchedulerJob.id == j.id)
                    .values(enabled=snapshot[j.name])
                )
            await db.commit()
            try:
                await pusher(db, stack_id, actor_id=actor_id)
            except Exception:
                # Do NOT mask the original error (if any). The DB is
                # restored; only the on-server file is stale, and the
                # next admin save / redeploy will re-assert it.
                logger.exception(
                    "scheduler restore push failed for stack=%s — DB is "
                    "restored but the on-server scheduler_config.json is "
                    "stale until the next save/redeploy",
                    stack_id,
                )


__all__ = ["disable_scheduler_for_stack"]
