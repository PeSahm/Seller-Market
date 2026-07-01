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
    # Earliest date the report/backfill queries the broker from. Set to the
    # start of the period the operator actually wants to bill from for the
    # currently-active accounts; the report then surfaces each account's true
    # first executed order at/after this date.
    "robot_start_date": "2026-05-19",
    # The bot fires at ~08:44:30 Tehran and orders land in this wall-clock
    # window at market open. Used as the default time-of-day filter on the
    # "Bot orders" tab and as the historical bot-attribution heuristic.
    "bot_window_start": "08:44:59",
    "bot_window_end": "08:45:03",
    # Instruments to EXCLUDE from the bot report + fee — one ISIN or symbol per
    # line (commas/semicolons also accepted). An order is excluded if its ISIN
    # OR symbol matches any entry (case-insensitive). Use it to keep bonds the
    # agents buy out of your report and fee. Empty = exclude nothing.
    "excluded_instruments": "",
    # --- Auto-balance + locust auto-scale (load-balance feature) ---
    # Auto-scale each stack's locust users/spawn to its customer-section count on
    # every render/push (fixes the fixed users=10 silently capping trading at 10
    # customers). "true"/"false".
    "enable_locust_autoscale": "true",
    # The "users = N× sections" multiplier (operator wants "at least 3×").
    "autobalance_users_multiplier": "3",
    # On every push, rebalance a multi-stack agent's customers across its servers
    # by section count (with hysteresis). Disable to keep customers where they are
    # while still auto-scaling locust. "true"/"false".
    "enable_autobalance": "true",
    # --- Market-data sidecar (issue #108) ---
    # Base URL of the per-host market-data sidecar this mgmt UI talks to. On the
    # mgmt host the sidecar runs as the ``market-data`` service in the same
    # compose project, reachable over the compose network. Powers the instrument
    # search/dropdown (#109) and the fee 20-day mark-to-market price (#111). A
    # connection error degrades gracefully (no dropdown results / no live price).
    "market_data_url": "http://market-data:8077",
    # --- Auto-sell (#110) ---
    # URL the BOTS' auto-sell monitor uses to reach the shared market-data WS
    # service (the per-host one published on PouyanIt, e.g.
    # "http://5.10.248.55:8077"). EMPTY (default) = auto-sell OFF fleet-wide: the
    # bot stacks keep the byte-identical scheduler-only command. Setting it makes
    # the next redeploy switch each stack to bot_entrypoint.py + MARKET_DATA_URL.
    "bot_market_data_url": "",
    # --- Loss fee on a manual close (#111 follow-up) ---
    # When a bot-bought position is CLOSED (a saved close price ≤ its avg buy,
    # i.e. in loss) on the Close-positions page, bill this FIXED fee per losing
    # position (customer × stock). GLOBAL default in TOMAN; per-agent override
    # lives in agent_fee_configs. The report converts ×10 to Rial. "0" = none.
    "mark_to_market_loss_fee_toman": "0",
    # --- Bot runtime / endpoints (DB-pushed [runtime] section) -------------
    # These used to be HARDCODED in the bot's Python image, so changing one (e.g.
    # the ephoenix market-data host moving mdapi1 -> marketdatagw) needed a full
    # CI + image-rebuild + fleet-redeploy cycle. They are now rendered into the
    # bot's config.ini ``[runtime]`` section and pushed to every stack instantly
    # (no CI, no image, no recreate). Each ``bot_rt_<suffix>`` row renders as the
    # wire key ``<suffix>`` the bot reads (``runtime_config.get``); EVERY default
    # equals the previous hardcoded literal, so behaviour is unchanged until
    # edited. ephoenix family:
    "bot_rt_ephoenix_domain": "ephoenix.ir",
    "bot_rt_ephoenix_md_host": "marketdatagw",   # the mdapi1 -> marketdatagw incident
    # ib (IbTrader) — its own domain + market-data host + portfolio shard:
    "bot_rt_ib_domain": "ibtrader.ir",
    "bot_rt_ib_md_host": "mdapi",
    "bot_rt_ib_portfolio_shard": "api8",
    # exir / Rayan-HamAfza family:
    "bot_rt_exir_domain": "exirbroker.com",
    "bot_rt_exir_fallback_buy_fee": "0.005",
    # auto-sell timing knobs (hot-reloaded by the monitor's supervisor). The
    # confirm-secs default is stored as "5.0" (not "5") so it round-trips through
    # the route's float field — str(5.0) == "5.0" — and an unchanged Save is
    # correctly detected as "no change" (no spurious fleet push).
    "bot_rt_auto_sell_window": "09:00-12:30",
    "bot_rt_auto_sell_confirm_secs": "5.0",
    # Mofid / Orbis firing knobs. Read by run_mofid / mofid_firer at FIRE time,
    # so a change applies on the next open after the fleet config push — no
    # redeploy. The firer creates N identical full-volume drafts and batch-sends
    # them in the [window_start, window_end] open window (more drafts = better
    # queue odds; can't over-buy, only one fills). Defaults == the bot's literals.
    "bot_rt_mofid_draft_count": "1",
    # run_time = when run_mofid starts (login + create the drafts), a minute
    # before the fire window. The Mofid scheduler reads it LIVE from [runtime]
    # each loop, so — like the window/draft_count knobs — a change applies on the
    # next open with NO redeploy.
    "bot_rt_mofid_run_time": "08:44:00",
    "bot_rt_mofid_window_start": "08:44:58.450",
    "bot_rt_mofid_window_end": "08:45:00.900",
}

# Settings whose value is rendered into the bot's config.ini ``[runtime]``
# section. ``bot_rt_*`` rows render under the suffix wire key; the two aliases
# below carry existing settings into [runtime] under the names the bot reads.
BOT_RUNTIME_PREFIX = "bot_rt_"
_BOT_RUNTIME_ALIASES: dict[str, str] = {
    # setting key -> [runtime] wire key
    "ocr_service_url": "ocr_service_url",
    "bot_market_data_url": "market_data_url",
}


def build_runtime_section(all_settings: dict[str, str]) -> dict[str, str]:
    """Project the settings dict onto the bot config.ini ``[runtime]`` wire dict.

    * ``bot_rt_<wire>`` rows render as ``<wire>`` (the name the bot's
      ``runtime_config.get`` reads). This covers both the validated first-class
      fields AND the Advanced raw-editor escape-hatch keys
      (``endpoint_*`` / ``exir_path_*`` / ``rlc_*`` / ``rlc_ws_*``).
    * ``ocr_service_url`` and ``bot_market_data_url`` also flow in (as
      ``ocr_service_url`` / ``market_data_url``) so the OCR pool + auto-sell feed
      become instantly changeable too.

    A value still at its built-in DEFAULT is OMITTED: the bot's hardcoded
    fallback is identical, so config.ini stays byte-for-byte the same as the
    pre-feature output until an operator actually changes something (no churn on
    the file that's pushed on every customer edit, and the renderer skips the
    whole section when nothing is overridden). Escape-hatch keys (no default) are
    rendered whenever set. Empty values are always omitted.
    """
    out: dict[str, str] = {}

    def _is_override(setting_key: str, value: object) -> bool:
        if value in (None, ""):
            return False
        return setting_key not in DEFAULTS or value != DEFAULTS[setting_key]

    for key, value in all_settings.items():
        if key.startswith(BOT_RUNTIME_PREFIX) and _is_override(key, value):
            out[key[len(BOT_RUNTIME_PREFIX):]] = value
    for setting_key, wire_key in _BOT_RUNTIME_ALIASES.items():
        value = all_settings.get(setting_key, "")
        if _is_override(setting_key, value):
            out[wire_key] = value
    return out


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
