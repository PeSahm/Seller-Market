"""Consolidated HA status (#156 WS4).

A single probe-on-load snapshot for the ``/admin/ha`` page so the operator is
never blind to what's up/down again after the PouyanIt incident. Everything is
**graceful** — a down probe target yields a red badge, never a 500 — and runs
on page load (no new table, no migration, no background worker).

Covers the shared-service SPOFs the HA plan addresses:
  * the **main DB** (now external) + the **warm spare** (recovery target),
  * the **OCR pool** (client-side failover endpoints),
  * server / bot-stack status rollups, unacked alert counts,
  * which mgmt instance holds the **worker-leader** lease (WS3).
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import db as db_mod
from app.models.health import HealthSignal
from app.models.servers import Server
from app.models.stacks import AgentStack
from app.services import settings_store
from app.services.broker_client import _ocr_base_urls
from app.services.db_backup import MANIFEST_NAME, load_manifest
from app.services.instance_heartbeat import list_instances
from app.settings import get_settings

logger = logging.getLogger(__name__)

_OCR_PROBE_TIMEOUT = 5.0
_SPARE_PROBE_TIMEOUT = 4.0


def _dsn_host_port(dsn: str) -> tuple[str, int | None]:
    """Best-effort (host, port) from a SQLAlchemy/libpq URL, driver suffix
    stripped so ``urlparse`` sees a plain scheme."""
    try:
        cleaned = (
            dsn.replace("+asyncpg", "").replace("+psycopg2", "").replace("+psycopg", "")
        )
        parsed = urlparse(cleaned)
        return (parsed.hostname or "?", parsed.port)
    except Exception:  # noqa: BLE001 — display-only, never raise
        return ("?", None)


async def _probe_main_db() -> dict:
    # Probe the MAIN engine directly (it is never rebound — only the shared
    # sessionmaker is, on failover). Probing the request session would report
    # the SPARE's reachability as the main's after a failover.
    host, port = _dsn_host_port(get_settings().database_url)
    t0 = time.perf_counter()

    async def _q() -> None:
        async with db_mod.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_q(), timeout=_SPARE_PROBE_TIMEOUT)
        return {
            "host": host,
            "port": port,
            "reachable": True,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("ha: main DB probe failed: %s", exc)
        return {"host": host, "port": port, "reachable": False, "latency_ms": None}


async def _probe_spare_db(spare_dsn: str) -> dict | None:
    """Probe the warm spare (recovery target) with its own short-lived asyncpg
    connection. Returns None when no spare is configured."""
    if not spare_dsn:
        return None
    host, port = _dsn_host_port(spare_dsn)
    raw = spare_dsn.replace("+asyncpg", "")
    try:
        import asyncpg  # local import: only needed when a spare is configured

        conn = await asyncio.wait_for(asyncpg.connect(raw), timeout=_SPARE_PROBE_TIMEOUT)
        try:
            t0 = time.perf_counter()
            await conn.execute("SELECT 1")
            latency = round((time.perf_counter() - t0) * 1000, 1)
        finally:
            await conn.close()
        return {"host": host, "port": port, "reachable": True, "latency_ms": latency}
    except Exception as exc:  # noqa: BLE001
        logger.warning("ha: spare DB probe failed: %s", exc)
        return {"host": host, "port": port, "reachable": False, "latency_ms": None}


async def _probe_ocr(urls: list[str]) -> list[dict]:
    """Probe each OCR endpoint. ``host.docker.internal`` is a host-local
    address (each bot host's own OCR) that the mgmt container can't resolve, so
    it's labelled rather than probed. Any HTTP response = the port answered =
    reachable; only a transport error is 'down'."""
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=_OCR_PROBE_TIMEOUT) as client:
        for url in urls:
            base = url.rstrip("/")
            if "host.docker.internal" in base:
                out.append(
                    {"url": base, "host_local": True, "reachable": None, "latency_ms": None}
                )
                continue
            t0 = time.perf_counter()
            try:
                resp = await client.get(base + "/")
                out.append(
                    {
                        "url": base,
                        "host_local": False,
                        "reachable": True,
                        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                        "status": resp.status_code,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                out.append(
                    {
                        "url": base,
                        "host_local": False,
                        "reachable": False,
                        "latency_ms": None,
                        "error": str(exc)[:120],
                    }
                )
    return out


def _backups_summary(settings) -> dict:
    """Read the on-disk backup manifest (graceful) for the Backups card.

    The manifest lives in ``backup_dir`` on the spare host and is the same file
    the recovery console reads — so this works with or without the main DB.
    """
    import os

    entries = load_manifest(os.path.join(settings.backup_dir, MANIFEST_NAME))
    latest = entries[-1] if entries else None
    return {
        "count": len(entries),
        "retention": settings.backup_retention,
        "dir": settings.backup_dir,
        "latest": (
            {
                "taken_at": latest.get("taken_at"),
                "restored_ok": latest.get("restored_ok"),
                "size": latest.get("size"),
            }
            if latest
            else None
        ),
    }


async def build_ha_status(
    db: AsyncSession,
    *,
    is_worker_leader: bool = True,
    failed_over_at=None,
) -> dict:
    """Build the full HA snapshot. Graceful throughout — any single probe
    failing degrades to a red/unknown badge, never raises."""
    settings = get_settings()
    active_db = db_mod.active_db()

    try:
        ocr_setting = await settings_store.get_setting(db, "ocr_service_url")
    except Exception:  # noqa: BLE001
        ocr_setting = ""
    ocr_urls = _ocr_base_urls(ocr_setting or settings.default_ocr_service_url)

    # The DB probes + OCR probes are independent: main DB uses ``db``, the spare
    # uses its own connection, OCR uses httpx — safe to run concurrently (only
    # one task touches ``db``).
    main_db, spare_db, ocr = await asyncio.gather(
        _probe_main_db(),
        _probe_spare_db(settings.spare_dsn),
        _probe_ocr(ocr_urls),
    )

    # Rollups (sequential, after the gather → ``db`` is free again).
    def _counts(rows) -> dict[str, int]:
        return {str(status): int(count) for status, count in rows}

    servers = _counts(
        (await db.execute(select(Server.status, func.count()).group_by(Server.status))).all()
    )
    stacks = _counts(
        (
            await db.execute(
                select(AgentStack.status, func.count()).group_by(AgentStack.status)
            )
        ).all()
    )
    alerts = _counts(
        (
            await db.execute(
                select(HealthSignal.severity, func.count())
                .where(HealthSignal.ack_at.is_(None))
                .group_by(HealthSignal.severity)
            )
        ).all()
    )

    try:
        instances = await list_instances(db)
    except Exception:  # noqa: BLE001 — display-only, never 500
        instances = []

    return {
        "main_db": main_db,
        "spare_db": spare_db,
        "ocr": ocr,
        "instances": instances,
        "instances_total": len(instances),
        "servers": servers,
        "servers_total": sum(servers.values()),
        "stacks": stacks,
        "stacks_total": sum(stacks.values()),
        "alerts": alerts,
        "alerts_attention": alerts.get("critical", 0) + alerts.get("error", 0),
        "is_worker_leader": is_worker_leader,
        "recovery_configured": bool(settings.spare_dsn),
        "active_db": active_db,
        "on_spare": active_db != "main",
        "failed_over_at": failed_over_at,
        "auto_failover_enabled": settings.enable_db_auto_failover,
        "backups": _backups_summary(settings),
    }
