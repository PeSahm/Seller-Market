"""Pydantic schemas for audit-log read endpoints (Phase 9).

The ``audit_log`` table is written from every mutating service
(scheduler-job upsert, run start/finalize, health signal ack, stack push,
customer create, server pin, settings update, ...). It's append-only —
nothing in the system rewrites or deletes rows — and the Phase-9 mgmt UI
surfaces it back to operators as a chronological feed plus a per-row
"before vs after" diff view.

We expose two outbound shapes:

* :class:`AuditOut` — projection of one ``audit_log`` row for the feed
  table and the per-row detail view. Includes the raw ``before_json`` /
  ``after_json`` payloads so the detail template can render them; the
  redaction + diff is done in :mod:`app.services.audit` before the
  payloads ever leave the service layer.
* :class:`AuditDiffEntry` — one entry in the flattened, dotted-path diff
  computed by :func:`app.services.audit.diff_json`. The UI's diff panel
  iterates over these to render an added/removed/changed table.

Neither schema lives on the write path: audit rows are written via the
``AuditLog`` ORM model directly from the producing services
(:mod:`app.services.runs`, :mod:`app.services.scheduler_jobs`, etc.).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AuditOut(BaseModel):
    """Outbound projection of an :class:`app.models.audit.AuditLog` row.

    Built directly from the ORM object via ``from_attributes=True``; the
    router code does ``AuditOut.model_validate(row)``. The two JSONB
    payloads are surfaced verbatim — redaction is handled in the service
    layer (:func:`app.services.audit.redact_payload`) so the same
    projection can be reused by callers that want the raw shape for
    diffing (which itself runs redaction internally).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor_user_id: Optional[UUID]
    action: str
    target_type: str
    target_id: str
    before_json: Optional[dict]
    after_json: Optional[dict]
    ts: datetime


class AuditDiffEntry(BaseModel):
    """One change in the before -> after diff.

    ``path`` is the dotted path from the document root to the changed
    leaf (e.g. ``"payload.password"`` or ``"keys[0].private_key"``).
    ``before`` is ``None`` for "added" keys and ``after`` is ``None`` for
    "removed" keys; ``change`` carries the explicit label so consumers
    don't have to re-derive it.

    Numbers, strings, bools, ``None``, and nested dicts/lists pass
    through unchanged into ``before`` / ``after`` — the diff is
    structural, not type-narrowed.
    """

    path: str
    before: object | None
    after: object | None
    change: str  # "added" | "removed" | "changed"


__all__ = [
    "AuditOut",
    "AuditDiffEntry",
]
