"""Parse and upsert trade-result JSON files from a stack's order_results/ dir.

The in-container trading bot writes one JSON dump per (account, broker) run
into ``<stack_dir>/order_results/{username}_{broker}_{YYYYMMDD_HHMMSS}.json``.
Each dump has shape::

    {
      "username": "...",
      "broker_code": "...",
      "timestamp": "ISO",
      "order_count": N,
      "orders": [ { "tracking_number": ..., "isin": ..., ... }, ... ]
    }

This module is the ingest side: per stack, every tick we

1. take a Postgres advisory lock keyed on the stack so two parallel
   workers can't double-insert,
2. SFTP-list the order_results/ directory, filtering against the
   :class:`app.models.runs.IngestCursor` row (``last_filename`` +
   ``last_mtime``) so we only touch new files,
3. fetch each file via SFTP, parse the JSON, resolve a
   :class:`app.models.customers.Customer` per order (the bot's
   per-(account, broker, isin, side) granularity matches Phase 1's
   composite UNIQUE), find or synthesise a :class:`app.models.runs.Run`
   to attach the order to,
4. ``INSERT ... ON CONFLICT DO NOTHING`` keyed on
   ``trade_results.tracking_number`` so retries are idempotent,
5. advance the cursor and commit — all in one transaction so a crash
   mid-tick either applies the whole batch or none of it.

Errors are logged and accumulated into the returned
:class:`IngestTickResult` rather than raised, because the caller is a
loop over many stacks and we don't want one broken stack to wedge the
others.

We don't import :mod:`paramiko` at module load — SFTP helpers are
imported lazily inside :func:`fetch_file` so the test harness can stub
us without dragging the SSH stack in.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import desc, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import hash_lock_key
from app.models.customers import Customer
from app.models.runs import IngestCursor, Run
from app.models.servers import Server
from app.models.stacks import AgentStack
from app.models.trade_instructions import TradeInstruction
from app.models.trades import TradeResult
from app.schemas.trade import IngestTickResult
from app.services.ssh.exceptions import SSHError

logger = logging.getLogger(__name__)

# Filename pattern: ``{username}_{broker}_{YYYYMMDD_HHMMSS}.json``.
# Broker codes are short ASCII alpha (``bbi``, ``mfb``, ...). Usernames in
# the bot range from numeric account ids to free-form aliases, so we allow
# letters, digits, underscores, and hyphens.
_FILENAME_RE = re.compile(
    r"^(?P<username>[A-Za-z0-9_\-]+)_(?P<broker>[a-z]+)_"
    r"(?P<ts>\d{8}_\d{6})\.json$"
)


@dataclass
class _RemoteFile:
    """One eligible ``order_results/*.json`` file on a stack server.

    ``mtime`` is parsed from ``stat -c '%Y'`` (epoch seconds) so we get a
    timezone-safe UTC datetime independent of the remote shell locale.
    ``full_path`` is what we hand back to SFTP; ``name`` is the bare
    basename we persist in the cursor for tie-breaking.
    """

    name: str
    mtime: datetime
    full_path: str


async def list_new_files(
    server: Server,
    stack: AgentStack,
    *,
    last_filename: Optional[str],
    last_mtime: Optional[datetime],
) -> list[_RemoteFile]:
    """Return ``order_results/*.json`` files strictly newer than the cursor.

    Strategy:

    * One remote ``stat -c '%Y %n' ...*.json`` so we get an epoch second
      plus full path per line — no separate ``ls`` + ``stat`` roundtrip,
      and ``%Y`` is locale-independent.
    * ``2>/dev/null || true`` swallows the "no such file" stderr when the
      directory exists but contains no ``*.json`` yet (``stat`` returns
      1 in that case).
    * ``last_mtime`` filter has a 5-second slop so a file written in the
      same second as the cursor's last tick isn't skipped due to
      filesystem mtime rounding. The exact ``last_filename`` filter
      below disambiguates same-second files.
    """
    # Lazy import of run_command keeps paramiko (which app.services.ssh.commands
    # eagerly imports at module load) out of the trade_ingestor module-load
    # path. Tests can then patch ``app.services.ssh.commands.run_command``
    # at the source and we'll see the patched function on the next call.
    from app.services.ssh.commands import run_command

    remote_dir = f"{stack.stack_dir}/order_results"
    stat_cmd = (
        f"stat -c '%Y %n' {shlex.quote(remote_dir)}/*.json "
        f"2>/dev/null || true"
    )
    result = await run_command(server, stat_cmd, timeout=30.0, check=False)
    files: list[_RemoteFile] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            mtime_str, full = line.split(" ", 1)
            mtime = datetime.fromtimestamp(int(mtime_str), tz=timezone.utc)
        except (ValueError, OSError):
            # Either ``stat`` printed an unparseable line or the epoch
            # was out of range for the local platform — skip it; the
            # cursor will pick the file up on the next tick if it's
            # legitimate.
            continue
        name = full.rsplit("/", 1)[-1]
        if not _FILENAME_RE.match(name):
            # Foreign files dropped into order_results/ (debug dumps,
            # editor swap files, ...) are filtered out so we don't try
            # to parse them as bot output.
            continue
        files.append(_RemoteFile(name=name, mtime=mtime, full_path=full))

    if last_mtime is not None:
        cutoff = last_mtime - timedelta(seconds=5)
        files = [f for f in files if f.mtime > cutoff]

    if last_filename is not None:
        # Precise filename filter — defends against equal-mtime files in
        # the same second that the mtime slop above would otherwise
        # re-admit.
        files = [f for f in files if f.name > last_filename]

    files.sort(key=lambda f: (f.mtime, f.name))
    return files


async def fetch_file(server: Server, remote_path: str) -> bytes:
    """SFTP-fetch one file and return raw bytes.

    Lazy import of :mod:`app.services.ssh.sftp` to keep paramiko out of
    the module-load path — the unit tests stub :func:`fetch_file` directly
    and shouldn't need the SSH stack on disk.
    """
    from app.services.ssh.sftp import sftp_read_text

    text_payload = await sftp_read_text(server, remote_path)
    return text_payload.encode("utf-8")


def _decimal_from(value: Any) -> Optional[Decimal]:
    """Coerce a JSON-ish numeric to :class:`Decimal`; return ``None`` on junk.

    The broker dumps mix native numbers and stringified numbers
    interchangeably (e.g. ``"1000"`` vs ``1000``); ``Decimal(str(...))``
    handles both. We swallow :class:`InvalidOperation` / :class:`ValueError`
    rather than raising so one weird order doesn't blow up the whole
    file — :func:`_upsert_trade` will fall back to ``Decimal("0")`` for
    the not-null ``price`` column.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_created(value: Optional[str]) -> Optional[datetime]:
    """Parse the broker's ``created`` ISO timestamp.

    The broker emits naive ISO strings (``"2026-05-17T08:30:01"`` or
    ``"...123456"`` with microseconds). We assume UTC for naive values —
    the bot runs in containers configured to UTC. Returns ``None`` on
    malformed input.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _resolve_customer(
    db: AsyncSession,
    *,
    agent_id: UUID,
    username: str,
    broker: str,
    isin: str,
    side: int,
) -> Optional[Customer]:
    """Match an order's (account + instrument) to a Customer row.

    Post-migration 0003, Customer is account-shaped (agent, broker,
    username); the (isin, side) lives on a separate TradeInstruction.
    The order needs both to be valid — we want to attach the
    TradeResult only when:

    1. The account exists for this agent ((agent, broker, username) →
       Customer), AND
    2. The customer has an enabled TradeInstruction for (isin, side).

    Returns the Customer when both checks pass; ``None`` otherwise.
    The ingestor treats ``None`` as "drop this order on the floor and
    log" — we can't insert a :class:`TradeResult` without a
    ``customer_id`` (the FK is RESTRICT). TradeResult still FKs to
    Customer (not to TradeInstruction) so the existing FK shape
    survives the migration unchanged.
    """
    # Step 1: find the account-level Customer row.
    customer_stmt = (
        select(Customer)
        .where(
            Customer.agent_id == agent_id,
            Customer.username == username,
            Customer.broker == broker,
        )
        .limit(1)
    )
    customer = (await db.execute(customer_stmt)).scalar_one_or_none()
    if customer is None:
        return None

    # Step 2: confirm there's a matching TradeInstruction. We don't
    # care about enabled here — if an order came back from a now-
    # disabled instruction it's still legitimate trade history that we
    # want to record. The Customer is the only thing the TradeResult
    # FKs to anyway.
    ti_stmt = (
        select(TradeInstruction.id)
        .where(
            TradeInstruction.customer_id == customer.id,
            TradeInstruction.isin == isin,
            TradeInstruction.side == side,
        )
        .limit(1)
    )
    has_ti = (await db.execute(ti_stmt)).scalar_one_or_none()
    if has_ti is None:
        return None

    return customer


async def _resolve_or_create_run(
    db: AsyncSession,
    *,
    stack: AgentStack,
    file_mtime: datetime,
    all_done: bool,
) -> tuple[Run, bool]:
    """Find the run that produced this file, or synthesise one.

    Match rule: the most recent :class:`Run` on this stack whose
    ``started_at`` is ``<= file_mtime``. That covers both the mgmt-UI-
    initiated run path (where we already have a row from
    :func:`app.services.runs.start_run`) and the in-container scheduler
    path (where the bot fires runs we don't know about).

    For the scheduler case we insert a synthetic ``trigger='scheduled'``
    row whose ``started_at`` and ``finished_at`` are both ``file_mtime``
    — close enough for telemetry; the exact bot-side timing is opaque to
    us. ``status`` reflects whether every order in the file reached the
    bot's "done" state.

    Returns ``(run, created)`` so the caller can update the
    ``synthetic_runs_created`` counter without re-deriving the bit from
    timestamps.
    """
    stmt = (
        select(Run)
        .where(Run.stack_id == stack.id, Run.started_at <= file_mtime)
        .order_by(desc(Run.started_at))
        .limit(1)
    )
    result = await db.execute(stmt)
    run = result.scalar_one_or_none()
    if run is not None:
        return run, False

    synthetic = Run(
        stack_id=stack.id,
        agent_id=stack.agent_id,
        job_name="run_trading",
        trigger="scheduled",
        started_at=file_mtime,
        finished_at=file_mtime,
        status="success" if all_done else "failed",
        exit_code=0 if all_done else None,
        log_blob_ref=None,
        log_blob_sha256=None,
    )
    db.add(synthetic)
    # Flush so synthetic.id is populated for the trade-row FK; the
    # commit lives in the outer ``ingest_stack_once`` so the whole
    # tick is one transaction.
    await db.flush()
    return synthetic, True


async def _upsert_trade(
    db: AsyncSession,
    *,
    run_id: UUID,
    customer_id: UUID,
    order: dict,
) -> bool:
    """Insert one ``trade_results`` row; return ``True`` if inserted.

    ``ON CONFLICT (tracking_number) DO NOTHING`` makes the operation
    idempotent — a re-run over the same file (or a duplicate dump
    from the broker, which we've seen in practice) is a no-op.

    Required columns get safe fallbacks: missing ``price`` becomes
    ``Decimal("0")`` because the column is non-null, missing
    ``state_desc`` becomes ``""``. We surface these via the
    ``orders_unmatched_customer`` / ``orders_inserted`` counters but
    don't try to validate the broker's schema — that's a different
    layer's job.
    """
    stmt = (
        pg_insert(TradeResult)
        .values(
            run_id=run_id,
            customer_id=customer_id,
            tracking_number=int(order.get("tracking_number") or 0),
            isin=order.get("isin") or "",
            symbol=order.get("symbol"),
            side=int(order.get("side") or 0),
            price=_decimal_from(order.get("price")) or Decimal("0"),
            volume=int(order.get("volume") or 0),
            executed_volume=int(order.get("executed_volume") or 0),
            state=int(order.get("state") or 0),
            state_desc=order.get("state_desc") or "",
            is_done=bool(order.get("is_done", False)),
            net_amount=_decimal_from(order.get("net_amount")),
            created_at_broker=_parse_created(order.get("created")),
            created_shamsi=order.get("created_shamsi"),
            raw_json=order,
        )
        .on_conflict_do_nothing(index_elements=[TradeResult.tracking_number])
        .returning(TradeResult.id)
    )
    result = await db.execute(stmt)
    inserted = result.scalar_one_or_none()
    return inserted is not None


async def ingest_stack_once(
    db: AsyncSession,
    *,
    stack_id: UUID,
) -> IngestTickResult:
    """One ingest tick for one stack. Acquires advisory lock — callers don't.

    Never raises: unexpected exceptions are caught and stuffed into the
    returned :attr:`IngestTickResult.error` field so a per-stack failure
    can't wedge the caller's loop over every stack.

    Transactional model: every state change inside the lock (synthetic
    run inserts, trade upserts, cursor advance) lives in the same
    transaction. We commit at the bottom on the happy path or roll back
    in the broad ``except``. The advisory lock is transaction-scoped, so
    a rollback releases it automatically.
    """
    summary = IngestTickResult(stack_id=stack_id)
    lock_key = hash_lock_key("ingest", str(stack_id))
    try:
        gate = await db.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_key}
        )
        if not gate.scalar():
            summary.error = "another ingest tick is in flight for this stack"
            return summary

        stack = await db.get(AgentStack, stack_id)
        if stack is None:
            summary.error = f"stack {stack_id} not found"
            return summary
        server = await db.get(Server, stack.server_id)
        if server is None:
            summary.error = f"server {stack.server_id} not found"
            return summary

        cursor = await db.get(IngestCursor, stack_id)
        last_filename = cursor.last_filename if cursor else None
        last_mtime = cursor.last_mtime if cursor else None

        try:
            files = await list_new_files(
                server,
                stack,
                last_filename=last_filename,
                last_mtime=last_mtime,
            )
        except SSHError as exc:
            summary.error = f"ssh list failed: {exc}"
            return summary

        summary.files_seen = len(files)
        if not files:
            return summary

        # Cursor advances only over the **contiguously successful** prefix
        # of the sorted file list. If file B mid-batch fails (corrupt JSON,
        # SFTP read error) but C succeeds, we MUST keep the cursor at A,
        # not jump past B to C — otherwise B would be filtered out forever
        # on the next tick by the f.name > last_filename guard and silently
        # lost. We still PROCESS C in the same tick (its _upsert_trade
        # rows are ON CONFLICT DO NOTHING, so re-ingesting it next tick
        # after B is fixed is a cheap no-op) but only ADVANCE the cursor
        # while every preceding file in the iteration has succeeded.
        max_filename = last_filename
        max_mtime = last_mtime
        had_failure = False
        for rf in files:
            try:
                raw = await fetch_file(server, rf.full_path)
                payload = json.loads(raw)
            except (SSHError, json.JSONDecodeError) as exc:
                # One bad file shouldn't stop the rest from being
                # processed THIS tick (idempotent upsert covers re-runs),
                # but it MUST stop the cursor from advancing past this
                # file — otherwise the failed file is gone forever from
                # the next tick's view.
                logger.warning(
                    "trade_ingestor: skip file=%s err=%s",
                    rf.full_path,
                    exc,
                )
                had_failure = True
                continue

            orders = payload.get("orders") or []
            if not orders:
                # Empty dumps are legitimate ("nothing to do this tick")
                # — count them; advance the cursor IF no preceding file
                # in this batch failed.
                summary.files_skipped_empty += 1
                if not had_failure:
                    if max_filename is None or rf.name > max_filename:
                        max_filename = rf.name
                    if max_mtime is None or rf.mtime > max_mtime:
                        max_mtime = rf.mtime
                continue

            broker = payload.get("broker_code") or ""
            username = payload.get("username") or ""
            all_done = all(bool(o.get("is_done", False)) for o in orders)
            run, created_synthetic = await _resolve_or_create_run(
                db,
                stack=stack,
                file_mtime=rf.mtime,
                all_done=all_done,
            )
            if created_synthetic:
                summary.synthetic_runs_created += 1
            run_id = run.id

            for order in orders:
                customer = await _resolve_customer(
                    db,
                    agent_id=stack.agent_id,
                    username=username,
                    broker=broker,
                    isin=order.get("isin") or "",
                    side=int(order.get("side") or 0),
                )
                if customer is None:
                    # We can't insert without a customer_id (FK is
                    # RESTRICT). Skip with a structured log so the
                    # operator can repair the missing Customer row.
                    summary.orders_unmatched_customer += 1
                    logger.info(
                        "trade_ingestor: unmatched customer agent=%s "
                        "user=%s broker=%s isin=%s side=%s — skipping order",
                        stack.agent_id,
                        username,
                        broker,
                        order.get("isin"),
                        order.get("side"),
                    )
                    continue
                try:
                    inserted = await _upsert_trade(
                        db,
                        run_id=run_id,
                        customer_id=customer.id,
                        order=order,
                    )
                except Exception:  # noqa: BLE001
                    # Upsert failure inside the tick is logged but
                    # doesn't abort — the surrounding transaction will
                    # commit the rest of the file's successful inserts.
                    logger.exception(
                        "trade_ingestor: upsert failed file=%s tracking=%s",
                        rf.full_path,
                        order.get("tracking_number"),
                    )
                    continue
                if inserted:
                    summary.orders_inserted += 1
                else:
                    summary.orders_duplicate += 1

            summary.files_ingested += 1
            # Same contiguous-success rule: only advance the cursor if
            # no preceding file in this batch failed. If had_failure is
            # True, this file's inserts have already landed (they're
            # idempotent via ON CONFLICT) and the next tick will re-see
            # the file, do a no-op upsert, and then attempt the cursor
            # advance again once the gap is closed.
            if not had_failure:
                if max_filename is None or rf.name > max_filename:
                    max_filename = rf.name
                if max_mtime is None or rf.mtime > max_mtime:
                    max_mtime = rf.mtime

        # Cursor update lives in the same transaction as the inserts so
        # we never have a "rows inserted but cursor not advanced" or
        # vice-versa state visible to other readers.
        if cursor is None:
            cursor = IngestCursor(
                stack_id=stack_id,
                last_filename=max_filename,
                last_mtime=max_mtime,
            )
            db.add(cursor)
        else:
            cursor.last_filename = max_filename
            cursor.last_mtime = max_mtime
            cursor.updated_at = datetime.now(timezone.utc)

        await db.commit()
        return summary

    except Exception as exc:  # noqa: BLE001
        logger.exception("trade_ingestor: unexpected error stack=%s", stack_id)
        summary.error = f"unexpected: {exc}"
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            # Already in an error path; suppressing a rollback failure
            # keeps the original cause as the surfaced error.
            pass
        return summary


__all__ = [
    "ingest_stack_once",
    "list_new_files",
    "fetch_file",
    "IngestTickResult",
]
