"""Worker leader election (#156 WS3).

With the database **external** (Session 23), mgmt can run on multiple hosts for
HA. But only ONE instance may run the background workers — they SSH the whole
fleet, so two live sets = double redeploys / double ingestion / a scheduler
firing twice. This elects a single leader via a Postgres **session-scoped
advisory lock** held for the app's lifetime on a dedicated connection: the
holder runs the workers; the others serve the UI only. If the leader process
dies, its connection drops and Postgres auto-releases the lock, so a restarted
standby can acquire it.

v1 is a **startup-time** election (a standby takes over the workers on its next
restart, not instantly — good enough to prevent the double-firing hazard).
**Fail-open**: if the lock check errors (e.g. a transient DB blip), assume
leadership so a single-instance deployment never silently loses its workers.
"""
from __future__ import annotations

import logging

from app.db import AsyncSessionLocal, hash_lock_key, try_acquire_session_lock

logger = logging.getLogger(__name__)

# Stable key for the single mgmt worker-leader lock (BLAKE2b -> int64).
WORKER_LEADER_LOCK_KEY = hash_lock_key("mgmt", "worker-leader")


async def acquire_worker_leadership(app) -> bool:
    """Try to become the worker leader.

    On success, HOLD the lock by keeping a dedicated ``AsyncSession`` open on
    ``app.state._leader_session`` for the app's lifetime (closing it, e.g. on
    process death, releases the session-scoped advisory lock). Returns whether
    this instance should run the background workers.
    """
    session = AsyncSessionLocal()
    try:
        acquired = await try_acquire_session_lock(session, WORKER_LEADER_LOCK_KEY)
    except Exception as exc:  # noqa: BLE001 — fail-open for single-instance safety
        logger.warning(
            "worker leader election errored (%s); assuming leadership", exc
        )
        await _safe_close(session)
        app.state._leader_session = None
        return True

    if acquired:
        app.state._leader_session = session  # keep open -> keep the lock held
        logger.info("worker leader: ACQUIRED — this instance runs the workers")
        return True

    await _safe_close(session)
    app.state._leader_session = None
    logger.info("worker leader: NOT acquired — another instance leads; UI-only here")
    return False


async def release_worker_leadership(app) -> None:
    """Release the held lock (close the dedicated session) on shutdown."""
    session = getattr(app.state, "_leader_session", None)
    if session is not None:
        await _safe_close(session)
        app.state._leader_session = None


async def _safe_close(session) -> None:
    try:
        await session.close()
    except Exception:  # noqa: BLE001 — best-effort
        pass
