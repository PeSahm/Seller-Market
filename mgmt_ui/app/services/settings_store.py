"""Thin helpers around the ``settings`` table.

The mgmt UI exposes a small set of admin-tunable knobs (OCR service URL, agent
stack image tag, ...). Their canonical home is the ``settings`` DB table; this
module provides typed get / set / get-all helpers that fall back to documented
defaults when a row is absent.

Why a tiny module instead of inlining queries in the router? Two reasons:

1. The defaults need to live next to the table accessor so admins, the
   renderer, and the workers all agree on a single source of truth.
2. Unit tests want to drive these helpers against an in-memory SQLite DB
   without spinning up the whole router.

The caller owns transaction commit semantics — :func:`set_setting` only stages
the row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.settings import Setting

# Documented defaults. Any new key must be added here AND to the admin form
# template (otherwise it won't be editable). The values mirror what's hard-
# coded in :mod:`app.settings` so a fresh install behaves identically before
# and after an admin first visits the settings page.
DEFAULTS: dict[str, str] = {
    "ocr_service_url": "http://5.10.248.55:18080",
    # Published trading-bot image — same one the existing root-level
    # deployment uses. See https://github.com/PeSahm/Seller-Market/pkgs/container/seller-market
    "agent_image_tag": "ghcr.io/pesahm/seller-market:latest",
    # Per-stack ``processes`` ceiling for locust runs (Phase 5). Stored as
    # the string form of an int so this dict can stay ``dict[str, str]`` —
    # callers that need the numeric value do their own ``int()`` parse
    # (see :func:`app.services.locust_configs._resolve_processes_cap`).
    # Default ``4`` was chosen as a conservative cap on a 32-core fleet
    # host: ~8 concurrent agents can each run a 4-process load without
    # over-subscribing the box.
    "agent_locust_processes_cap": "4",
    # --- Bot orders + profit-share fee report (issue: GetOrders report) ---
    # Operator's profit-share fee as a PERCENT (so "1.0" == 1%). This is the
    # GLOBAL default; per-agent overrides live in ``agent_fee_configs``.
    "profit_fee_percent": "1.0",
    # Earliest date the report/backfill queries the broker from. Code for
    # automated order placement first shipped 2025-11-06; first proven live
    # order 2025-11-29 — 2025-11-01 is a safe lower bound. The report then
    # surfaces each account's true first executed order.
    "robot_start_date": "2025-11-01",
    # The bot fires at ~08:44:30 Tehran and orders land in this wall-clock
    # window at market open. Used as the default time-of-day filter on the
    # "Bot orders" tab and as the historical bot-attribution heuristic.
    "bot_window_start": "08:44:59",
    "bot_window_end": "08:45:03",
}


async def get_setting(db: AsyncSession, key: str) -> str:
    """Return the stored value, or the documented default if absent.

    Raises:
        KeyError: if ``key`` is not in :data:`DEFAULTS` and has no DB row.
            That's a programmer error (typo in a caller), not a user-facing
            situation, so we surface it loudly.
    """
    result = await db.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    if row is not None:
        return row.value
    if key in DEFAULTS:
        return DEFAULTS[key]
    raise KeyError(f"unknown setting key: {key!r}")


async def set_setting(
    db: AsyncSession,
    key: str,
    value: str,
    *,
    updated_by: Optional[UUID] = None,
) -> Setting:
    """Upsert a setting row and emit a matching audit-log entry.

    The caller is responsible for ``db.commit()``.

    On insert we set ``updated_at`` explicitly even though the column has a
    ``server_default`` — that way the value is correct when the test harness
    runs against SQLite (which doesn't honour the PG ``now()`` default).

    Phase 9: every call also writes an ``audit_log`` row with
    ``action="setting.update"``, ``target_type="setting"``, and
    ``target_id=<key>``. The ``before_json`` / ``after_json`` payloads
    carry ``{"value": <prev>}`` / ``{"value": <new>}`` so the admin
    audit detail page can diff them. We emit the audit row even when
    the new value matches the previous one — the existing pattern is
    "admins can always see that this update happened" (the diff just
    surfaces as empty in that case). For a brand-new key the
    ``before_json`` value is ``None``, which renders in the diff as
    an "added" entry.

    The audit row is ``db.add``-ed before the upsert is staged; the
    caller's later ``db.commit()`` flushes both atomically.
    """
    result = await db.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    prev_value: Optional[str] = row.value if row is not None else None
    now = datetime.now(timezone.utc)

    # Stage the audit row first. It references the key only by string —
    # ``target_id`` is ``Text`` in the schema, not a UUID — so the
    # ordering vs the upsert doesn't matter for FK correctness.
    db.add(
        AuditLog(
            actor_user_id=updated_by,
            action="setting.update",
            target_type="setting",
            target_id=str(key),
            before_json={"value": prev_value},
            after_json={"value": value},
            ts=now,
        )
    )

    if row is None:
        row = Setting(
            key=key,
            value=value,
            updated_by=updated_by,
            updated_at=now,
        )
        db.add(row)
    else:
        row.value = value
        row.updated_by = updated_by
        row.updated_at = now
    return row


async def get_all_settings(db: AsyncSession) -> dict[str, str]:
    """Return every known setting (DB rows + defaults for unset keys).

    The DB rows win over defaults — admins see what's actually in effect, not
    a mix of "what's stored" and "what would be stored if nothing was set".
    """
    result = await db.execute(select(Setting))
    rows = {r.key: r.value for r in result.scalars().all()}
    out: dict[str, str] = dict(DEFAULTS)
    out.update(rows)
    return out
