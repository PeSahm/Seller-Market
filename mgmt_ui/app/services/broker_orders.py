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
(``"4580090306"``); we assert that as a sanity check but always attribute to
the queried customer (the broker occasionally returns a parent pamCode).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import desc, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.broker_orders import BrokerOrder
from app.models.customers import Customer
from app.security import crypto
from app.services import broker_client

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


def map_getorders_row(row: dict, customer: Customer) -> dict:
    """Map one raw GetOrders row to ``broker_orders`` column values.

    Attribution: ``customer`` is the account we queried with, so it owns
    every row. We still record ``pam_code`` for audit.
    """
    return {
        "customer_id": customer.id,
        "agent_id": customer.agent_id,
        "broker": customer.broker,
        "account_username": customer.username,
        "pam_code": (str(row.get("pamCode")) if row.get("pamCode") is not None else None),
        "tracking_number": int(row.get("trackingNumber") or 0),
        "broker_order_id": (int(row["id"]) if row.get("id") is not None else None),
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
        "placed_at": _parse_dt(row.get("date")),
        "created_at_broker": _parse_dt(row.get("created")),
        "execution_date": _parse_dt(row.get("executionDate")),
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
            result.pam_mismatches += 1
            logger.warning(
                "pamCode %s does not end with username %s (broker %s) — "
                "attributing to queried customer anyway",
                values["pam_code"], customer.username, customer.broker,
            )
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
    "raw_json",
)

# Money/price columns where a NULL in a later fetch must NOT clobber a
# previously-good value — a malformed re-fetch would otherwise null out the
# price and silently corrupt the fee report. COALESCE keeps the old value.
_COALESCE_ON_CONFLICT = frozenset(
    {"price", "total_fee", "executed_amount", "net_traded_value"}
)


async def _upsert_order(db: AsyncSession, values: dict) -> bool:
    """Insert one ``broker_orders`` row; ``True`` if newly inserted, else
    ``False`` (an existing row was updated).

    ``ON CONFLICT (tracking_number) DO UPDATE`` keeps the row fresh as the
    broker fills the order across polls. ``fetched_at`` is bumped to now()
    on every update so the operator can see staleness.

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
        index_elements=[BrokerOrder.tracking_number],
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


__all__ = [
    "FetchResult",
    "fetch_and_upsert_orders",
    "map_getorders_row",
    "list_orders",
    "in_time_window",
    "refresh_orders_for_customers",
    "reconcile_all_recent",
]
