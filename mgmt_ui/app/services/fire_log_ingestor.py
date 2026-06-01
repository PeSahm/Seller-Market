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

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import hash_lock_key
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
