"""Per-instance heartbeat for the HA page (#156).

Each running mgmt instance upserts its own ``mgmt_instances`` row every
~15s to the SHARED database (the one currently active — main, or the spare
after a failover). Any instance's ``/admin/ha`` page then lists all instances
with which one holds worker leadership and which DB each is serving.

Runs on EVERY instance (not leader-gated — the whole point is to surface the
standbys too).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import db as db_mod
from app.db import AsyncSessionLocal
from app.models.mgmt_instance import MgmtInstance
from app.settings import get_settings

logger = logging.getLogger(__name__)


async def upsert_instance(
    db, *, name: str, address, version, active_db: str, is_leader: bool
) -> None:
    stmt = pg_insert(MgmtInstance).values(
        name=name,
        address=address,
        version=version,
        active_db=active_db,
        is_leader=is_leader,
        last_seen_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["name"],
        set_={
            "address": address,
            "version": version,
            "active_db": active_db,
            "is_leader": is_leader,
            "last_seen_at": func.now(),
        },
    )
    await db.execute(stmt)
    await db.commit()


async def list_instances(db, *, stale_after_seconds: int = 60) -> list[dict]:
    """All heartbeat rows (newest-name order) with a ``stale`` flag for any
    instance not seen within ``stale_after_seconds``."""
    rows = (
        await db.execute(select(MgmtInstance).order_by(MgmtInstance.name))
    ).scalars().all()
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for r in rows:
        age = (now - r.last_seen_at).total_seconds() if r.last_seen_at else 1e9
        out.append(
            {
                "name": r.name,
                "address": r.address,
                "version": r.version,
                "active_db": r.active_db,
                "is_leader": bool(r.is_leader),
                "last_seen_at": r.last_seen_at,
                "stale": age > stale_after_seconds,
            }
        )
    return out


async def run_heartbeat_worker(app, stop_event: asyncio.Event) -> None:
    settings = get_settings()
    interval = settings.instance_heartbeat_interval_seconds
    name = settings.resolved_instance_name()
    address = settings.mgmt_instance_address or None
    # Settings has no ``app_version`` field (the dashboard hardcodes it) — use a
    # safe getattr so a missing attr can't crash the task before its loop.
    version = getattr(settings, "app_version", None)
    logger.info("instance heartbeat started as '%s' (every %ss)", name, interval)
    while not stop_event.is_set():
        try:
            async with AsyncSessionLocal() as db:
                await upsert_instance(
                    db,
                    name=name,
                    address=address,
                    version=version,
                    active_db=db_mod.active_db(),
                    is_leader=bool(getattr(app.state, "is_worker_leader", True)),
                )
        except Exception as exc:  # noqa: BLE001 — never let the heartbeat die
            logger.warning("instance heartbeat upsert failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("instance heartbeat stopped")
