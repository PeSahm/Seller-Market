"""Fetch + store + read broker order history (the mgmt UI's own GetOrders feed).

This is the write+read side of :class:`app.models.broker_orders.BrokerOrder`.
Unlike :mod:`app.services.trade_ingestor` (which SFTPs the bot's
``order_results/*.json`` — open-orders only), this module calls the broker
``GetOrders`` API DIRECTLY via :func:`app.services.broker_client.get_orders`
and so sees fully-executed buys AND sells. That closes the "trade page misses
trades" gap and provides the data the profit/fee report runs on.

Attribution is implicit + authoritative: we query GetOrders per customer using
THAT customer's own decrypted token, so every returned row is that customer's.
``pamCode`` (e.g. ``"33094580090306"``) ends with the account/username
(``"4580090306"``); a row whose pamCode does NOT end with the queried username
is a foreign account's order and is SKIPPED (storing it under the queried
customer would mis-attribute money data).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import bindparam, delete, desc, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.broker_orders import BrokerOrder
from app.models.customers import Customer
from app.security import crypto
from app.services import broker_client
from app.services.brokers._jalali import parse_jalali_datetime
from app.services.brokers.registry import UnknownBrokerError, family_of

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """Outcome of a per-customer GetOrders fetch+upsert."""

    customer_id: UUID
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0  # rows dropped for an unusable tracking_number
    pam_mismatches: int = 0
    error: Optional[str] = None


def _decimal_from(value: Any) -> Optional[Decimal]:
    """Coerce a JSON-ish numeric to :class:`Decimal`; ``None`` on junk.

    Same lenient parse as :func:`app.services.trade_ingestor._decimal_from` —
    the broker mixes native numbers and stringified numbers.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse a broker ISO timestamp (``"2026-06-01T08:45:00.657"``).

    The broker emits naive wall-clock strings. We label them UTC (matching
    :func:`app.services.trade_ingestor._parse_created`) so the column is
    tz-aware — but note the WALL-CLOCK time-of-day is preserved, which is
    what the market-open window filter compares against (see
    :func:`_in_time_window`). Returns ``None`` on malformed input.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Dedup-key date when a broker row carries no usable timestamp at all (rare /
# malformed). A fixed sentinel keeps placed_date NOT NULL without inventing a
# fake "today" that would change between fetches of the same order.
_PLACED_DATE_SENTINEL = date(1970, 1, 1)


def _derive_placed_date(
    placed_at: Optional[datetime],
    created_at_broker: Optional[datetime],
    execution_date: Optional[datetime],
) -> date:
    """Placement DATE for the dedup key (migration 0015).

    Stored broker timestamps are Tehran wall-clock labeled UTC, so ``.date()``
    IS the Tehran market date — the same basis the fire-log date reconcile
    uses. Tracking numbers are broker-day sequences; without the date in the
    key, the same number on a different day would clobber a different order.
    """
    dt = placed_at or created_at_broker or execution_date
    return dt.date() if dt is not None else _PLACED_DATE_SENTINEL


def map_getorders_row(row: dict, customer: Customer) -> dict:
    """Map one raw GetOrders row to ``broker_orders`` column values.

    Family-aware dispatcher: the wire shape of the row differs by broker
    family (ephoenix vs exir), but both map onto the SAME ``broker_orders``
    column set. We resolve the family from the customer's broker code via the
    warm registry cache and route to the matching mapper. An unknown code
    (cache miss / brand-new broker) falls back to the ephoenix shape, which
    is the historical default.

    Attribution: ``customer`` is the account we queried with, so it owns
    every row regardless of family. We still record ``pam_code`` for audit.
    """
    try:
        fam = family_of(customer.broker)
    except UnknownBrokerError:
        fam = "ephoenix"
    if fam == "exir":
        return _map_exir_row(row, customer)
    return _map_ephoenix_row(row, customer)


def _map_ephoenix_row(row: dict, customer: Customer) -> dict:
    """Map one ephoenix ``GetOrders`` row to ``broker_orders`` column values.

    This is the original, unchanged ephoenix mapping (the only family before
    Exir). Field names match the ephoenix GetOrders wire shape.
    """
    placed_at = _parse_dt(row.get("date"))
    created_at_broker = _parse_dt(row.get("created"))
    execution_date = _parse_dt(row.get("executionDate"))
    return {
        "customer_id": customer.id,
        "agent_id": customer.agent_id,
        "broker": customer.broker,
        "account_username": customer.username,
        "pam_code": (str(row.get("pamCode")) if row.get("pamCode") is not None else None),
        "tracking_number": int(row.get("trackingNumber") or 0),
        "broker_order_id": (int(row["id"]) if row.get("id") is not None else None),
        "serial_number": (
            int(row["serialNumber"]) if row.get("serialNumber") is not None else None
        ),
        "isin": row.get("isin") or "",
        "symbol": row.get("symbol") or None,
        "symbol_title": row.get("symbolTitle") or None,
        "order_side": int(row.get("orderSide") or 0),
        "price": _decimal_from(row.get("price")),
        "volume": int(row.get("volume") or 0),
        "executed_volume": int(row.get("executedVolume") or 0),
        "total_fee": _decimal_from(row.get("totalFee")),
        "executed_amount": _decimal_from(row.get("executedAmount")),
        "net_traded_value": _decimal_from(row.get("netTradedValue")),
        "state": int(row.get("state") or 0),
        "state_desc": row.get("stateDesc") or None,
        "is_done": bool(row.get("isDone", False)),
        "placed_at": placed_at,
        "placed_date": _derive_placed_date(placed_at, created_at_broker, execution_date),
        "created_at_broker": created_at_broker,
        "execution_date": execution_date,
        "raw_json": row,
    }


def _int_or_zero(value: Any) -> int:
    """Coerce a JSON-ish numeric to ``int``; 0 on junk/None (mirrors the
    ephoenix mapper's ``int(row.get(...) or 0)`` tolerance for the Exir
    rows, whose quantities arrive as native numbers but may be absent)."""
    if value is None:
        return 0
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return 0


def _map_exir_row(row: dict, customer: Customer) -> dict:
    """Map one Exir ``orderbookReport`` row to ``broker_orders`` column values.

    Returns the SAME dict shape as :func:`_map_ephoenix_row` so the upsert and
    the downstream report don't care which family produced the row. Field
    sources are the Exir wire keys documented in ``scratch/EXIR_FINDINGS.md``.

    Exir has no global serial in the orderbook report (``serial_number`` =
    ``None``) and no separate broker order id distinct from the dedup key, so
    ``broker_order_id`` mirrors ``mmtpOrderId``. Datetimes arrive as Jalali
    ``"YYYY/MM/DD-HH:mm:ss"`` strings and are parsed to tz-aware Gregorian via
    :func:`parse_jalali_datetime`.
    """
    tracking = _int_or_zero(row.get("mmtpOrderId"))
    isin = row.get("insMaxLCode") or ""
    # orderSideName: "خريد"/"خرید" = buy (خ), "فروش" = sell (ف). Match on the
    # first letter so the Arabic-vs-Persian yeh spelling doesn't matter.
    side_name = str(row.get("orderSideName", ""))
    # Map ONLY explicit prefixes: خ = buy (خريد/خرید), ف = sell (فروش). Anything
    # else (blank / unexpected) → 0 = unknown side. 0 won't be matched as a buy
    # or a sell by profit matching, which is safer than defaulting an unknown
    # value to a (false) sell.
    order_side = 1 if side_name.startswith("خ") else (2 if side_name.startswith("ف") else 0)
    # entryDateTime is the only timestamp Exir gives us; use it for placement
    # AND sub-second placement (no separate "created"). Filled rows have no
    # explicit execution timestamp in the report → execution_date stays None.
    # parse_jalali_datetime returns a Tehran-aware (+03:30) datetime. Re-label it
    # UTC WITHOUT shifting the wall-clock numerals so it matches the ephoenix
    # convention (_parse_dt also stores Tehran wall-clock labeled UTC). Otherwise
    # the two families' placed_at would be 3.5h apart in absolute terms and the
    # UTC-boundary date-range filters (list_orders / build_fee_report) would
    # classify the same local date inconsistently near midnight.
    entry_dt = parse_jalali_datetime(row.get("entryDateTime") or "")
    if entry_dt is not None:
        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
    # remainingQuantity == 0 means fully filled (ephoenix's isDone equivalent).
    is_done = _int_or_zero(row.get("remainingQuantity")) == 0
    return {
        "customer_id": customer.id,
        "agent_id": customer.agent_id,
        "broker": customer.broker,
        "account_username": customer.username,
        # Exir's accountNumber ends with the username — same role as ephoenix
        # pamCode (audit only; attribution is by the queried customer).
        "pam_code": (
            str(row.get("accountNumber")) if row.get("accountNumber") is not None else None
        ),
        "tracking_number": tracking,
        # No distinct broker order id field; reuse the dedup key for the
        # cross-reference column rather than leave it null.
        "broker_order_id": tracking or None,
        "serial_number": None,  # Exir orderbookReport carries no serialNumber
        "isin": isin,
        # insMaxLCode IS the ISIN; Exir has no short ticker in this feed, so we
        # use the same code for ``symbol`` and the Persian name for the title.
        "symbol": isin or None,
        "symbol_title": row.get("farsiName") or None,
        "order_side": order_side,
        "price": _decimal_from(row.get("price")),
        "volume": _int_or_zero(row.get("quantity")),
        "executed_volume": _int_or_zero(row.get("tradedQuantity")),
        # Exir reports no per-order fee in the orderbook; the fee report
        # COALESCEs NULLs, so None is safe (won't be coerced to 0).
        "total_fee": None,
        "executed_amount": _decimal_from(row.get("totalValue")),
        "net_traded_value": _decimal_from(row.get("pureValue")),
        # We only ingest filled rows for the report; stamp state=3 to satisfy
        # profit_report's ``state == 3`` filter (matches EXIR_FINDINGS note).
        "state": 3,
        "state_desc": row.get("mmtpOrderStatusName") or None,
        "is_done": is_done,
        "placed_at": entry_dt,
        "placed_date": _derive_placed_date(entry_dt, None, None),
        "created_at_broker": entry_dt,
        "execution_date": None,  # no execution timestamp in the orderbook feed
        "raw_json": row,
    }


async def fetch_and_upsert_orders(
    db: AsyncSession,
    customer: Customer,
    *,
    from_date: date,
    to_date: date,
    ocr_service_url: str,
    include_status: Optional[list[int]] = None,
) -> FetchResult:
    """Pull one customer's order history and upsert into ``broker_orders``.

    Decrypts the customer's password, calls GetOrders (paginated), and
    upserts each row with ``ON CONFLICT (tracking_number) DO UPDATE`` so a
    re-fetch refreshes the mutable fields (state / executed_volume / fees)
    of an order that has filled further since the last poll, without ever
    duplicating it. The caller owns ``db.commit()``.

    Errors are captured in :class:`FetchResult`, not raised — the caller
    sweeps many customers and one bad account must not wedge the rest.
    """
    result = FetchResult(customer_id=customer.id)
    try:
        password = crypto.decrypt(customer.password_enc)
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the sweep
        result.error = f"could not decrypt credentials: {exc}"
        return result

    rows, err = await broker_client.get_orders(
        broker_code=customer.broker,
        username=customer.username,
        password=password,
        ocr_service_url=ocr_service_url,
        from_date=from_date.strftime("%Y/%m/%d"),
        to_date=to_date.strftime("%Y/%m/%d"),
        include_status=include_status if include_status is not None else [3],
    )
    result.fetched = len(rows)
    if err:
        result.error = err

    for row in rows:
        values = map_getorders_row(row, customer)
        if values["tracking_number"] <= 0:
            # No usable dedup key — skip rather than collide on 0. Count it so
            # the loss is visible in the per-customer log, not silent.
            result.skipped += 1
            logger.warning(
                "skipping order with non-positive tracking_number for %s@%s (isin=%s)",
                customer.username, customer.broker, values.get("isin"),
            )
            continue
        if values["pam_code"] and not values["pam_code"].endswith(customer.username):
            # A row whose pamCode doesn't end with the queried username belongs
            # to a DIFFERENT account — storing it under this customer would
            # mis-attribute money data (the pre-0015 collision corruption was
            # exactly this shape). Skip it; the true owner's own fetch stores it.
            result.pam_mismatches += 1
            logger.warning(
                "skipping foreign-account row: pamCode %s does not end with "
                "username %s (broker %s, trk %s)",
                values["pam_code"], customer.username, customer.broker,
                values["tracking_number"],
            )
            continue
        inserted = await _upsert_order(db, values)
        if inserted:
            result.inserted += 1
        else:
            result.updated += 1

    return result


# Columns refreshed on a re-fetch. We deliberately EXCLUDE the immutable
# identity/placement fields (customer_id, isin, order_side, placed_at,
# created_at_broker, first_seen_at) so a poll only updates what can change as
# an order fills.
_MUTABLE_ON_CONFLICT = (
    "executed_volume",
    "volume",
    "price",
    "total_fee",
    "executed_amount",
    "net_traded_value",
    "state",
    "state_desc",
    "is_done",
    "execution_date",
    "symbol",
    "symbol_title",
    "serial_number",
    "raw_json",
)

# Columns where a NULL in a later fetch must NOT clobber a previously-good
# value — a malformed re-fetch would otherwise null out the price (corrupting
# the fee report) or the serial (breaking fire-log reconciliation). COALESCE
# keeps the old value. ``serial_number`` is here too so a re-fetch backfills
# it on rows stored before the column existed, without ever wiping it.
_COALESCE_ON_CONFLICT = frozenset(
    {"price", "total_fee", "executed_amount", "net_traded_value", "serial_number"}
)


async def _upsert_order(db: AsyncSession, values: dict) -> bool:
    """Insert one ``broker_orders`` row; ``True`` if newly inserted, else
    ``False`` (an existing row was updated).

    ``ON CONFLICT (broker, account_username, tracking_number, placed_date) DO
    UPDATE`` keeps the row fresh as the broker fills the order across polls.
    ``fetched_at`` is bumped to now() on every update so the operator can see
    staleness.

    Insert-vs-update is detected with the PostgreSQL ``(xmax = 0)`` idiom:
    a freshly inserted tuple has ``xmax = 0``; a tuple updated via DO UPDATE
    carries a non-zero ``xmax``. (We can't compare ``first_seen_at`` to
    ``fetched_at`` — ``now()`` is constant within a transaction, so two
    upserts in one txn would look identical.)
    """
    stmt = pg_insert(BrokerOrder).values(**values)
    set_ = {}
    for col in _MUTABLE_ON_CONFLICT:
        excluded = getattr(stmt.excluded, col)
        if col in _COALESCE_ON_CONFLICT:
            # Never overwrite a stored money value with a NULL from a bad poll.
            set_[col] = func.coalesce(excluded, getattr(BrokerOrder, col))
        else:
            set_[col] = excluded
    set_["fetched_at"] = func.now()
    stmt = stmt.on_conflict_do_update(
        # Dedup key (migration 0015): tracking numbers are broker-DAY sequence
        # numbers that repeat across accounts and days. Only the same REAL
        # order (same broker + account + number + placement date) may conflict
        # — anything looser lets one customer's refresh overwrite another
        # customer's (or the same customer's other-day) row.
        index_elements=[
            BrokerOrder.broker,
            BrokerOrder.account_username,
            BrokerOrder.tracking_number,
            BrokerOrder.placed_date,
        ],
        set_=set_,
    ).returning(literal_column("(xmax = 0)"))
    inserted = (await db.execute(stmt)).scalar_one()
    return bool(inserted)


def _as_time(value: Any) -> Optional[time]:
    """Coerce ``"HH:MM:SS"`` / :class:`time` to a :class:`time`; ``None`` on junk."""
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    try:
        return time.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def in_time_window(
    order: BrokerOrder, window_start: Optional[time], window_end: Optional[time]
) -> bool:
    """True if the order's placement WALL-CLOCK time-of-day is within the window.

    Compares the time-of-day of ``created_at_broker`` (falling back to
    ``placed_at``) against ``[window_start, window_end]`` inclusive. Because
    both bounds and the stored timestamp are the broker's wall clock, no tz
    conversion is needed — this is what isolates the bot's market-open burst
    (08:44:59–08:45:03). A null window bound means "open-ended" on that side.
    """
    if window_start is None and window_end is None:
        return True
    ts = order.created_at_broker or order.placed_at
    if ts is None:
        return False
    tod = ts.time()
    if window_start is not None and tod < window_start:
        return False
    if window_end is not None and tod > window_end:
        return False
    return True


def parse_exclusions(raw: Optional[str]) -> set[str]:
    """Parse a multi-line / comma / semicolon list of ISINs or symbols into a
    normalized (upper-cased, trimmed) set. Empty input → empty set."""
    if not raw:
        return set()
    parts = re.split(r"[\n,;]+", raw)
    return {p.strip().upper() for p in parts if p.strip()}


def is_excluded(order: BrokerOrder, exclude: set[str]) -> bool:
    """True if the order's ISIN, symbol, or symbol_title is in the exclusion
    set (case-insensitive). Used to keep e.g. agent-bought bonds out of the
    report and fee."""
    if not exclude:
        return False
    for v in (order.isin, order.symbol, order.symbol_title):
        if v and v.strip().upper() in exclude:
            return True
    return False


async def list_orders(
    db: AsyncSession,
    *,
    agent_id: Optional[UUID] = None,
    customer_id: Optional[UUID] = None,
    broker: Optional[str] = None,
    symbol_or_isin: Optional[str] = None,
    side: Optional[int] = None,
    state: Optional[int] = None,
    since: Optional[date] = None,
    until: Optional[date] = None,
    window_start: Optional[time] = None,
    window_end: Optional[time] = None,
    only_bot: bool = False,
    exclude: Optional[set[str]] = None,
    limit: int = 1000,
) -> list[BrokerOrder]:
    """Filter ``broker_orders`` for the report; placement-time descending.

    The coarse date range (``since``/``until`` on ``placed_at``) and the
    structural filters run in SQL; the precise market-open time-of-day
    window runs in Python (see :func:`in_time_window`) to avoid tz-conversion
    pitfalls on a ``timestamptz`` column. ``limit`` caps the SQL pull BEFORE
    the window filter, so widen the date range rather than relying on a huge
    limit when window-filtering historical data.
    """
    stmt = (
        select(BrokerOrder)
        .order_by(desc(BrokerOrder.placed_at), desc(BrokerOrder.tracking_number))
        .limit(limit)
    )
    if agent_id is not None:
        stmt = stmt.where(BrokerOrder.agent_id == agent_id)
    if customer_id is not None:
        stmt = stmt.where(BrokerOrder.customer_id == customer_id)
    if broker:
        stmt = stmt.where(BrokerOrder.broker == broker)
    if symbol_or_isin:
        stmt = stmt.where(
            (BrokerOrder.symbol == symbol_or_isin)
            | (BrokerOrder.isin == symbol_or_isin)
        )
    if side is not None:
        stmt = stmt.where(BrokerOrder.order_side == side)
    if state is not None:
        stmt = stmt.where(BrokerOrder.state == state)
    if since is not None:
        stmt = stmt.where(BrokerOrder.placed_at >= datetime.combine(since, time.min, tzinfo=timezone.utc))
    if until is not None:
        stmt = stmt.where(BrokerOrder.placed_at <= datetime.combine(until, time.max, tzinfo=timezone.utc))
    if only_bot:
        stmt = stmt.where(BrokerOrder.is_bot.is_(True))

    rows = list((await db.execute(stmt)).scalars().all())
    if window_start is not None or window_end is not None:
        rows = [r for r in rows if in_time_window(r, window_start, window_end)]
    if exclude:
        rows = [r for r in rows if not is_excluded(r, exclude)]
    return rows


# Bound concurrent broker logins during a fleet refresh — each login may
# cost a captcha+OCR solve (~5s) and we don't want to hammer the OCR service
# or the broker. The 30-min token cache amortises repeat customers.
_REFRESH_CONCURRENCY = 3


async def _refresh_one(
    customer_id: UUID, from_date: date, to_date: date, ocr_service_url: str
) -> None:
    """Fetch+upsert one customer in its OWN session, then commit.

    A dedicated session per customer is required because the fleet refresh
    runs several of these concurrently and :class:`AsyncSession` is not safe
    for concurrent use. Committing per customer means partial progress
    survives even if a later account errors.
    """
    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        customer = await db.get(Customer, customer_id)
        if customer is None:
            return
        res = await fetch_and_upsert_orders(
            db,
            customer,
            from_date=from_date,
            to_date=to_date,
            ocr_service_url=ocr_service_url,
        )
        await db.commit()
        logger.info(
            "bot-report refresh customer=%s fetched=%d ins=%d upd=%d skip=%d pam_mismatch=%d err=%s",
            customer_id, res.fetched, res.inserted, res.updated,
            res.skipped, res.pam_mismatches, res.error,
        )


async def refresh_orders_for_customers(
    customer_ids: list[UUID], *, from_date: date, to_date: date
) -> None:
    """Background entry point: refresh GetOrders for many customers.

    Fired fire-and-forget from the ``/admin/bot-report/refresh`` route via
    ``asyncio.create_task`` so the HTTP request returns immediately. Reads
    the OCR service URL once, then sweeps customers under a small concurrency
    semaphore. Never raises — per-customer failures are logged and skipped.
    """
    from app.db import AsyncSessionLocal
    from app.services import settings_store

    try:
        async with AsyncSessionLocal() as db:
            ocr_service_url = await settings_store.get_setting(db, "ocr_service_url")
    except Exception:  # noqa: BLE001 — honour the "never raises" contract
        logger.exception(
            "bot-report refresh: could not load OCR service URL; aborting sweep"
        )
        return

    sem = asyncio.Semaphore(_REFRESH_CONCURRENCY)

    async def _guarded(cid: UUID) -> None:
        async with sem:
            try:
                await _refresh_one(cid, from_date, to_date, ocr_service_url)
            except Exception:  # noqa: BLE001 — one bad account must not wedge the sweep
                logger.exception("bot-report refresh failed for customer=%s", cid)

    await asyncio.gather(*[_guarded(cid) for cid in customer_ids])


async def reconcile_all_recent(
    *, lookback_days: int = 3, today: Optional[date] = None
) -> int:
    """Daily auto-reconcile: pull a rolling recent window of GetOrders for
    EVERY customer into ``broker_orders``. Returns the number of customers
    swept.

    Pulls ``[today - lookback_days, today]`` inclusive — i.e. ``lookback_days``
    of overlap BEFORE today on top of today itself. The overlap is intentional:
    it guarantees the boundary day is never missed across a timezone/cron
    seam. The full historical backfill (from the robot start date) is the
    operator-triggered "Refresh from broker" action, not this loop. Idempotent:
    existing rows are refreshed via DO UPDATE.
    """
    from app.db import AsyncSessionLocal

    if today is None:
        today = date.today()
    from_date = today - timedelta(days=max(0, lookback_days))

    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(Customer.id))
        ids = [r[0] for r in rows.all()]

    if ids:
        await refresh_orders_for_customers(ids, from_date=from_date, to_date=today)
    return len(ids)


# Rows corrupted by the pre-0015 dedup-key collisions: the last conflicting
# write replaced the mutable columns (incl. raw_json) while the identity
# columns kept the FIRST writer's values — so any disagreement between the
# stored identity and the raw payload marks a clobbered row. Scoped to
# ephoenix-shaped payloads (the only family observed colliding; Exir raw rows
# carry none of these keys).
_CONTAMINATED_ROWS_SQL = """
    SELECT id, broker
    FROM broker_orders
    WHERE jsonb_exists(raw_json, 'pamCode')
      AND (
        (raw_json->>'pamCode' IS NOT NULL
         AND right(raw_json->>'pamCode', length(account_username))
             <> account_username)
        OR (raw_json->>'isin' IS NOT NULL AND raw_json->>'isin' <> isin)
        OR (left(raw_json->>'date', 10) ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
            AND left(raw_json->>'date', 10)::date <> placed_date)
      )
"""


async def repair_collision_rows(db: AsyncSession, *, dry_run: bool = False) -> dict:
    """One-time repair for rows corrupted by the pre-0015 dedup collisions.

    Deletes every contaminated row (its data belongs to a different order —
    the true owner's next refresh re-fetches it cleanly under the 0015 key;
    data belonging to accounts that aren't customers doesn't belong here at
    all) and resets ``order_fires.reconciled`` for customers of the affected
    brokers so the fire-log reconciler re-tags ``is_bot`` on the re-fetched
    rows (the tagging SQLs only consider unreconciled fires; re-tagging is
    idempotent).

    The caller then runs :func:`refresh_orders_for_customers` for the returned
    ``affected_customer_ids`` over a deep window to restore the originals.
    ``dry_run=True`` reports what WOULD be deleted without writing. Commits.
    """
    rows = (await db.execute(text(_CONTAMINATED_ROWS_SQL))).all()
    per_broker: dict[str, int] = {}
    contaminated_ids = []
    for r in rows:
        per_broker[r.broker] = per_broker.get(r.broker, 0) + 1
        contaminated_ids.append(r.id)
    affected_brokers = sorted(per_broker)

    affected_customer_ids: list[UUID] = []
    if affected_brokers:
        res = await db.execute(
            select(Customer.id).where(
                func.lower(Customer.broker).in_([b.lower() for b in affected_brokers])
            )
        )
        affected_customer_ids = [r[0] for r in res.all()]

    summary = {
        "dry_run": dry_run,
        "contaminated_per_broker": per_broker,
        "deleted": 0,
        "fires_reset": 0,
        "affected_brokers": affected_brokers,
        "affected_customer_ids": affected_customer_ids,
    }
    if dry_run or not contaminated_ids:
        return summary

    await db.execute(
        delete(BrokerOrder).where(BrokerOrder.id.in_(contaminated_ids))
    )
    summary["deleted"] = len(contaminated_ids)

    if affected_customer_ids:
        res = await db.execute(
            text(
                "UPDATE order_fires SET reconciled = false "
                "WHERE reconciled = true AND customer_id IN :cids"
            ).bindparams(bindparam("cids", expanding=True)),
            {"cids": affected_customer_ids},
        )
        summary["fires_reset"] = res.rowcount or 0

    await db.commit()
    logger.info(
        "repair_collision_rows: deleted=%d per_broker=%s fires_reset=%d",
        summary["deleted"], per_broker, summary["fires_reset"],
    )
    return summary


__all__ = [
    "FetchResult",
    "fetch_and_upsert_orders",
    "map_getorders_row",
    "list_orders",
    "in_time_window",
    "parse_exclusions",
    "is_excluded",
    "refresh_orders_for_customers",
    "reconcile_all_recent",
    "repair_collision_rows",
]
