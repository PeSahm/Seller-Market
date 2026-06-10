"""Ingest the bot's order fire-log + reconcile it against broker_orders.

The bot appends one JSONL line per account per run to
``run_results/order_fires_<YYYYMMDD>.jsonl`` recording which customer/broker/
isin/side it fired an order for (see ``SellerMarket/locustfile_new.py::
_emit_order_fire``). This service, per stack, SFTP-reads those files and
UPSERTs each line into :class:`app.models.order_fires.OrderFire`, deduping on
the bot-generated ``fire_uid`` (``ON CONFLICT DO NOTHING``) — the file is an
append-log that grows across the session, so we re-read it each tick and rely
on the unique key rather than deleting it (the janitor prunes old files).

A separate, DB-only :func:`reconcile_unreconciled` pass then tags
``broker_orders.is_bot=true`` for the executed buys that match a fire
(customer + isin + side + trading date). It runs every tick because the
matching ``broker_orders`` rows usually arrive LATER than the fire (the fire is
emitted pre-open; the executions are pulled by the broker reconciler / a manual
refresh afterward). Marking the fire ``reconciled`` stops it being re-checked.

Mirrors :mod:`app.services.scheduled_run_ingestor`'s per-stack structure.
"""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import hash_lock_key
from app.models.auto_sell_reload_status import AutoSellReloadStatus
from app.models.customers import Customer
from app.models.order_fires import OrderFire
from app.models.servers import Server
from app.models.stacks import AgentStack
from app.services.ssh.exceptions import SSHError

logger = logging.getLogger(__name__)

_SUPPORTED_SCHEMA = 1
_FIRE_GLOB = "order_fires_*.jsonl"
# Only re-read the most recent few daily files each tick. Older fires are
# already in the DB (deduped on fire_uid), so re-reading months of logs would
# be pure waste. Filenames are order_fires_YYYYMMDD.jsonl, which sort
# chronologically by name.
_MAX_RECENT_FILES = 7


@dataclass
class IngestResult:
    stack_id: UUID
    files_seen: int = 0
    rows_inserted: int = 0
    rows_skipped: int = 0
    errors: list[str] = field(default_factory=list)


async def _list_fire_files(server: Server, stack: AgentStack) -> list[str]:
    """List ``run_results/order_fires_*.jsonl`` full paths via one SSH call."""
    from app.services.ssh.commands import run_command

    remote_dir = f"{stack.stack_dir}/run_results"
    cmd = f"ls -1 {shlex.quote(remote_dir)}/{_FIRE_GLOB} 2>/dev/null || true"
    result = await run_command(server, cmd, timeout=20.0, check=False)
    paths = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    # Keep only the most recent files (filenames sort chronologically).
    paths.sort()
    return paths[-_MAX_RECENT_FILES:]


async def _fetch_text(server: Server, path: str) -> Optional[str]:
    from app.services.ssh.sftp import sftp_read_text

    try:
        return await sftp_read_text(server, path)
    except SSHError as exc:
        logger.warning("order_fires read failed for %s: %s", path, exc)
        return None


def _parse_fired_at(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _resolve_customer_id(
    db: AsyncSession, *, agent_id: UUID, broker: str, username: str
) -> Optional[UUID]:
    """Match a fire to its Customer by (agent, broker, username) only — NO
    TradeInstruction gate (a fire can be any isin/side, incl. sells)."""
    stmt = (
        select(Customer.id)
        .where(
            Customer.agent_id == agent_id,
            Customer.broker == broker,
            Customer.username == username,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _upsert_fire(db: AsyncSession, values: dict) -> bool:
    """Insert one ``order_fires`` row; ``True`` if newly inserted.

    ``ON CONFLICT (fire_uid) DO NOTHING`` — re-reading the growing append-log
    is a no-op for lines we've already seen.
    """
    stmt = (
        pg_insert(OrderFire)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[OrderFire.fire_uid])
        .returning(OrderFire.id)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


def _row_values(rec: dict, *, agent_id: UUID, customer_id: Optional[UUID]) -> Optional[dict]:
    """Map one parsed JSONL record to ``order_fires`` columns; ``None`` if invalid."""
    if rec.get("schema_version") != _SUPPORTED_SCHEMA:
        return None
    fire_uid = rec.get("fire_uid")
    username = rec.get("username")
    broker = rec.get("broker_code")
    isin = rec.get("isin")
    fired_at = _parse_fired_at(rec.get("fired_at"))
    if not (fire_uid and username and broker and isin and fired_at):
        return None
    try:
        side = int(rec.get("side"))
    except (TypeError, ValueError):
        return None
    def _opt_int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "customer_id": customer_id,
        "agent_id": agent_id,
        "broker": str(broker),
        "account_username": str(username),
        "isin": str(isin),
        "side": side,
        "fired_at": fired_at,
        "run_date": fired_at.date(),
        "fire_uid": str(fire_uid),
        "serial_number": _opt_int(rec.get("serial_number")),
        "tracking_number": _opt_int(rec.get("tracking_number")),
        "raw_json": rec,
    }


_STATUS_FILE = "auto_sell_status.json"


def _status_rows_from_marker(body: str, stack_id: UUID) -> Optional[list[dict]]:
    """Parse an ``auto_sell_status.json`` body → ``auto_sell_reload_status`` rows.

    Returns ``None`` when the marker is unusable (bad JSON / wrong schema /
    missing ``applied_at``) so the caller leaves existing rows untouched; an
    EMPTY list is a valid "the bot has nothing armed" state (disarm-all).
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != _SUPPORTED_SCHEMA:
        return None
    applied_at = _parse_fired_at(data.get("applied_at"))
    armed = data.get("armed")
    if applied_at is None or not isinstance(armed, list):
        return None

    # Last-wins dedup on the PK (stack_id, account, isin): a well-formed marker
    # has one entry per target, but a duplicate must NEVER turn into a bulk-
    # INSERT PK violation (which would poison the surrounding transaction).
    by_key: dict[tuple, dict] = {}
    for entry in armed:
        if not isinstance(entry, dict):
            continue
        isin = entry.get("isin")
        account = entry.get("account")
        try:
            threshold = int(entry.get("threshold"))
        except (TypeError, ValueError):
            continue
        if not isin or not account:
            continue
        by_key[(str(account), str(isin))] = {
            "stack_id": stack_id,
            "account": str(account),
            "isin": str(isin),
            "applied_threshold": threshold,
            "applied_at": applied_at,
        }
    return list(by_key.values())


async def _ingest_reload_status(db: AsyncSession, server: Server, stack: AgentStack) -> None:
    """Pull the bot's ``auto_sell_status.json`` marker → ``auto_sell_reload_status``.

    The bot overwrites this single small file each time its hot-reload supervisor
    APPLIES a config change (#110). We replace this stack's rows with the
    marker's current armed set so the Active-auto-sell page can show the
    operator which thresholds the LIVE bot has applied. A missing file (older
    bot / no reload yet) is a silent no-op — existing rows are left as-is.
    """
    from app.services.ssh.sftp import sftp_read_text

    path = f"{stack.stack_dir}/run_results/{_STATUS_FILE}"
    try:
        body = await sftp_read_text(server, path)
    except SSHError:
        return  # no marker yet / unreadable — normal, stay quiet
    rows = _status_rows_from_marker(body, stack.id)
    if rows is None:
        return
    # Replace this stack's status with the marker's full armed set (the marker
    # is authoritative + overwritten, so a disarmed isin should drop out).
    await db.execute(
        delete(AutoSellReloadStatus).where(AutoSellReloadStatus.stack_id == stack.id)
    )
    if rows:
        await db.execute(pg_insert(AutoSellReloadStatus).values(rows))


async def ingest_stack_once(db: AsyncSession, *, stack_id: UUID) -> IngestResult:
    """Read + upsert every order-fire line for one stack. Never raises."""
    result = IngestResult(stack_id=stack_id)
    lock_key = hash_lock_key("fire_log_ingest", str(stack_id))
    try:
        gate = await db.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_key}
        )
        if not gate.scalar():
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
            files = await _list_fire_files(server, stack)
        except SSHError as exc:
            await db.rollback()
            result.errors.append(f"ssh list failed: {exc}")
            return result
        result.files_seen = len(files)

        # Cache customer-id resolution per (broker, username) within the tick.
        cust_cache: dict[tuple[str, str], Optional[UUID]] = {}
        for path in files:
            body = await _fetch_text(server, path)
            if body is None:
                result.errors.append(f"read failed: {path}")
                continue
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    result.rows_skipped += 1
                    continue
                if not isinstance(rec, dict):
                    result.rows_skipped += 1
                    continue
                broker = str(rec.get("broker_code") or "")
                username = str(rec.get("username") or "")
                key = (broker, username)
                if key not in cust_cache:
                    cust_cache[key] = await _resolve_customer_id(
                        db, agent_id=stack.agent_id, broker=broker, username=username
                    )
                values = _row_values(
                    rec, agent_id=stack.agent_id, customer_id=cust_cache[key]
                )
                if values is None:
                    result.rows_skipped += 1
                    continue
                if await _upsert_fire(db, values):
                    result.rows_inserted += 1
                else:
                    result.rows_skipped += 1

        # Best-effort: pull the auto-sell hot-reload status marker too. The
        # SAVEPOINT is load-bearing: a failure inside (e.g. an unexpected DB
        # error) rolls back ONLY the status delete+insert — without it the
        # session would be poisoned and the commit below would raise, losing
        # every order-fire row staged in this txn.
        try:
            async with db.begin_nested():
                await _ingest_reload_status(db, server, stack)
        except Exception:  # noqa: BLE001
            logger.exception("auto_sell reload-status ingest failed for %s", stack_id)

        await db.commit()
        return result
    except Exception as exc:  # noqa: BLE001 — one stack must not wedge the loop
        logger.exception("fire_log_ingest unexpected failure for %s", stack_id)
        result.errors.append(f"unexpected: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return result


# Reconciliation tags ``broker_orders.is_bot`` for executions that match a
# fire two ways:
#   1. SERIAL-EXACT — the bot captured the broker serialNumber from its
#      NewOrder response; match it WITHIN THE SAME CUSTOMER. serialNumber is
#      not guaranteed globally unique across brokers, so scoping to the fire's
#      customer_id (which pins one broker + one account) prevents cross-broker/
#      cross-account mis-attribution while keeping the precise link.
#   2. DATE-BASED — match (customer, isin, side, trading-date). The bot is the
#      one firing at market open for that instrument, so all same-day executed
#      orders for that (customer, isin, side) are its — this catches fires with
#      no serial (queue-style responses) and any sibling spam executions.
# ``placed_at`` is the broker wall-clock stored as a UTC-labelled naive time.
_RECONCILE_TAG_SERIAL_SQL = text(
    """
    UPDATE broker_orders bo
    SET is_bot = true
    FROM order_fires f
    WHERE f.reconciled = false
      AND f.serial_number IS NOT NULL
      AND f.customer_id IS NOT NULL
      AND bo.customer_id = f.customer_id
      AND bo.serial_number = f.serial_number
      AND bo.is_bot = false
    """
)
_RECONCILE_TAG_DATE_SQL = text(
    """
    UPDATE broker_orders bo
    SET is_bot = true
    FROM order_fires f
    WHERE f.reconciled = false
      AND f.customer_id IS NOT NULL
      AND bo.customer_id = f.customer_id
      AND bo.isin = f.isin
      AND bo.order_side = f.side
      AND (bo.placed_at AT TIME ZONE 'UTC')::date = f.run_date
      AND bo.is_bot = false
    """
)
_RECONCILE_MARK_SQL = text(
    """
    UPDATE order_fires f
    SET reconciled = true
    WHERE f.reconciled = false
      AND (
        (f.serial_number IS NOT NULL AND f.customer_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM broker_orders bo
            WHERE bo.customer_id = f.customer_id
              AND bo.serial_number = f.serial_number))
        OR
        (f.customer_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM broker_orders bo
            WHERE bo.customer_id = f.customer_id
              AND bo.isin = f.isin
              AND bo.order_side = f.side
              AND (bo.placed_at AT TIME ZONE 'UTC')::date = f.run_date))
      )
    """
)


async def reconcile_unreconciled(db: AsyncSession) -> int:
    """Tag ``broker_orders.is_bot`` for executions matching an unreconciled
    fire (serial-exact + date-based), then mark those fires reconciled. Returns
    the number of fires reconciled. DB-only — safe to run every tick.

    The matching ``broker_orders`` usually arrive after the fire (the fire is
    emitted at market close; executions are pulled by the broker reconciler /
    a manual refresh), so a fire stays unreconciled until its executions land,
    then this tags them.
    """
    await db.execute(_RECONCILE_TAG_SERIAL_SQL)
    await db.execute(_RECONCILE_TAG_DATE_SQL)
    res = await db.execute(_RECONCILE_MARK_SQL)
    await db.commit()
    return res.rowcount or 0


__all__ = ["IngestResult", "ingest_stack_once", "reconcile_unreconciled"]
