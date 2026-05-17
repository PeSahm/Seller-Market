"""Pydantic schemas for health-signal endpoints (Phase 8).

The ``health_signals`` table holds one row per anomaly the scanner extracts
from a stack's ``trading_bot.log`` tail — rate-limits, captcha failures,
auth failures, broker timeouts, etc. The scanner service
(:mod:`app.services.health_signals`) writes them with a 60-minute dedup
window per ``(stack_id, kind)`` so a chronic problem doesn't flood the
table; the read service projects them back for the admin / agent UI.

We expose two outbound shapes here:

* :class:`HealthSignalOut` — projection of one ``health_signals`` row for
  the list view and the per-signal detail. Includes ``raw`` (the matching
  log line) and a derived ``is_acked`` convenience flag.
* :class:`HealthScanResult` — purely operational, summarises one scanner
  tick for one stack. Never crosses the HTTP boundary in user-facing
  responses; the worker logs it and surfaces counters to metrics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, computed_field

HealthSeverity = Literal["info", "warning", "error", "critical"]


class HealthSignalOut(BaseModel):
    """Outbound projection of a ``health_signals`` row for the UI tables.

    Built directly from the ORM object via ``from_attributes=True``; the
    router code does ``HealthSignalOut.model_validate(row)``. ``raw`` is
    included on both list and detail views since it's already truncated to
    2000 chars by the scanner — cheap to ship and useful for at-a-glance
    triage in the list table.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    stack_id: Optional[UUID]
    kind: str
    severity: str
    message: str
    raw: Optional[str]
    ts: datetime
    ack_by: Optional[UUID]
    ack_at: Optional[datetime]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_acked(self) -> bool:
        """``True`` iff an operator has acknowledged this signal.

        ``@computed_field`` makes Pydantic v2 include this in
        ``model_dump()`` / FastAPI serialisation — a bare ``@property``
        would be silently dropped from JSON responses.
        """
        return self.ack_at is not None


class HealthScanResult(BaseModel):
    """Summary of one scanner tick for one stack — used in logs and metrics.

    The scanner never raises out of its main entry point; it captures
    unrecoverable errors in :attr:`error` so the caller (a scheduled
    worker that iterates over every stack) can keep going. All counters
    default to 0 so a tick that ends early via an early-return still
    yields a valid summary.
    """

    stack_id: UUID
    lines_scanned: int = 0
    signals_inserted: int = 0
    signals_bumped: int = 0
    error: Optional[str] = None


__all__ = [
    "HealthSeverity",
    "HealthSignalOut",
    "HealthScanResult",
]
