"""Pydantic schemas for trade-result endpoints (Phase 7).

The ``trade_results`` table holds one row per broker order returned by the
in-container trading bot's order_results/ JSON dumps. The ingestor service
(:mod:`app.services.trade_ingestor`) parses those dumps off SFTP and upserts
into this table on a ``tracking_number`` unique key; the read service
(:mod:`app.services.trades`) projects rows back for the admin / agent UI.

We expose two outbound shapes:

* :class:`TradeOut` — the list-page projection. Same scalar columns as the
  model but no ``raw_json`` (it can be hundreds of bytes per row and we
  don't want to ship it across the wire for a 200-item table).
* :class:`TradeDetailOut` — extends :class:`TradeOut` with ``raw_json`` so
  the per-trade detail view can render a "view as JSON" pane.

The third shape, :class:`IngestTickResult`, is purely operational: it
summarises one ingest pass for one stack and is logged / surfaced to
metrics. It never crosses the HTTP boundary in user-facing responses.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TradeOut(BaseModel):
    """Outbound projection of a ``trade_results`` row for the UI tables.

    Built directly from the ORM object via ``from_attributes=True``; the
    router code does ``TradeOut.model_validate(row)``. We deliberately
    omit ``raw_json`` here — see :class:`TradeDetailOut` for the variant
    that includes it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    customer_id: UUID
    tracking_number: int
    isin: str
    symbol: Optional[str]
    side: int
    price: Decimal
    volume: int
    executed_volume: int
    state: int
    state_desc: str
    is_done: bool
    net_amount: Optional[Decimal]
    created_at_broker: Optional[datetime]
    created_shamsi: Optional[str]
    ingested_at: datetime


class TradeDetailOut(TradeOut):
    """Detail view also includes the raw JSON for "view as JSON" pane.

    ``raw_json`` mirrors the per-order dict from the bot's dump. It's
    typed ``Any`` because the bot's broker drivers occasionally emit
    extra fields that we don't enumerate in the model and we want to
    surface them verbatim to the operator.
    """

    raw_json: Any


class IngestTickResult(BaseModel):
    """Summary of one ingest pass for a stack — used in logs and metrics.

    The ingestor never raises out of its main entry point; it captures
    unrecoverable errors in :attr:`error` so the caller (a scheduled
    worker that iterates over every stack) can keep going. All counters
    default to 0 so a tick that ends early via an early-return still
    yields a valid summary.
    """

    stack_id: UUID
    files_seen: int = 0
    files_ingested: int = 0
    files_skipped_empty: int = 0
    orders_inserted: int = 0
    orders_duplicate: int = 0
    orders_unmatched_customer: int = 0
    synthetic_runs_created: int = 0
    error: Optional[str] = None


__all__ = [
    "TradeOut",
    "TradeDetailOut",
    "IngestTickResult",
]
