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

from app.models.settings import Setting

# Documented defaults. Any new key must be added here AND to the admin form
# template (otherwise it won't be editable). The values mirror what's hard-
# coded in :mod:`app.settings` so a fresh install behaves identically before
# and after an admin first visits the settings page.
DEFAULTS: dict[str, str] = {
    "ocr_service_url": "http://5.10.248.55:18080",
    # Default points at the locally-built image that `docker compose build`
    # produces from SellerMarket/Dockerfile on each trading server. This works
    # out-of-the-box on any server where the existing root-level deployment
    # has been built (matches the image name in SellerMarket/docker-compose.yml).
    # Switch this to a registry tag (e.g. ghcr.io/<org>/<repo>:<tag>) once you
    # publish a slim scheduler-only image.
    "agent_image_tag": "sellermarket-trading-bot:latest",
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
    """Upsert a setting row. The caller is responsible for ``db.commit()``.

    On insert we set ``updated_at`` explicitly even though the column has a
    ``server_default`` — that way the value is correct when the test harness
    runs against SQLite (which doesn't honour the PG ``now()`` default).
    """
    result = await db.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
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
