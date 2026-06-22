"""Phase 5 integration: scheduler_jobs + locust_config rows → rendered JSON.

These tests pin the full chain from the DB layer (faked) through
:func:`app.services.stacks._build_render_context` and out to the pure
renderers :func:`render_scheduler_config` / :func:`render_locust_config`.

We deliberately avoid spinning up a live DB or any SSH layer:

* ``db.get(Server, ...)`` is faked via :class:`unittest.mock.MagicMock`.
* ``db.execute`` is faked the same way — the two ``_read_setting`` reads
  and the ``_load_stack_customers`` SELECT all funnel through the same
  ``side_effect`` sequence (same pattern as
  :mod:`tests.unit.test_render_with_customers`).
* The scheduler-jobs and locust-config service entry points are
  monkeypatched at the module the stacks service imports them from
  (``app.services.scheduler_jobs.list_jobs`` and
  ``app.services.locust_configs.get_locust_config``) — that's the
  lazy-import surface :func:`_build_render_context` reaches through.

The four tests cover the four expected branches:

1. Two enabled jobs → both appear, top-level ``"enabled": True``.
2. All jobs disabled → top-level ``"enabled": True`` (per-job flags stay
   False). The top-level controls poll cadence, not job firing; we keep
   it on so a re-enable is picked up within 1s — see
   :func:`_build_render_context` for the rationale.
3. Custom :class:`LocustConfig` row → renderer emits its values verbatim.
4. No :class:`LocustConfig` row → renderer emits the documented defaults.
"""

from __future__ import annotations

import json
import uuid
from datetime import time as time_type
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import stacks as stacks_svc
from app.services.rendering import render_locust_config, render_scheduler_config


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _fake_stack(stack_id: uuid.UUID | None = None) -> SimpleNamespace:
    """Stack stand-in shaped for ``_build_render_context``."""
    return SimpleNamespace(
        id=stack_id or uuid.uuid4(),
        server_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        stack_dir="/root/seller-market/agents/abc",
    )


def _fake_server() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        base_dir="/root/seller-market/agents",
    )


def _fake_scheduler_job(
    *,
    name: str,
    time_str: str,
    enabled: bool,
    command: str,
) -> SimpleNamespace:
    """Minimal ``SchedulerJob`` stand-in.

    The render-context loader reads only ``name``, ``time``, ``enabled``, and
    ``command``. ``time`` is the python ``datetime.time`` that the DB column
    materialises; the seam in :func:`_build_render_context` is where we
    coerce it into ``"HH:MM:SS"`` via ``.strftime`` so we hand back a real
    :class:`datetime.time` here to exercise that coercion.
    """
    hh, mm, ss = (int(p) for p in time_str.split(":"))
    return SimpleNamespace(
        name=name,
        time=time_type(hh, mm, ss),
        enabled=enabled,
        command=command,
    )


def _fake_locust_config(
    *,
    users: int = 25,
    spawn_rate: int = 5,
    run_time: str = "300s",
    host: str = "https://example.test",
    processes: int = 4,
) -> SimpleNamespace:
    """Minimal ``LocustConfig`` stand-in for the renderer projection."""
    return SimpleNamespace(
        users=users,
        spawn_rate=spawn_rate,
        run_time=run_time,
        host=host,
        processes=processes,
    )


def _make_db(customer_rows: list[SimpleNamespace] | None = None) -> MagicMock:
    """Build a MagicMock that quacks like an ``AsyncSession``.

    ``_build_render_context`` issues, in order:

    * one ``db.get(Server, stack.server_id)`` — returns a fake server.
    * two ``db.execute`` for the two ``_read_setting`` calls
      (``agent_image_tag`` and ``ocr_service_url``).
    * one ``db.execute`` for ``_load_stack_customers`` — we hand back an
      empty list by default because these tests are not about customers.

    The scheduler-job and locust-config service calls go through a
    separately-monkeypatched module surface, not the DB, so they don't
    consume from this side_effect queue.
    """
    db = MagicMock()

    settings_result = MagicMock()
    settings_result.scalar_one_or_none = MagicMock(return_value=None)

    # The locust auto-scale toggle (read at the tail of _build_render_context)
    # is forced OFF here so these tests keep asserting the override-verbatim /
    # fleet-default locust behaviour; the auto-scale path is covered by
    # test_autobalance.py.
    autoscale_off = MagicMock()
    autoscale_off.scalar_one_or_none = MagicMock(return_value="false")

    customers_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=customer_rows or [])
    customers_result.scalars = MagicMock(return_value=scalars_mock)

    # get_all_settings (bot [runtime]) — empty rows → DEFAULTS → no overrides.
    all_settings_result = MagicMock()
    all_settings_scalars = MagicMock()
    all_settings_scalars.all = MagicMock(return_value=[])
    all_settings_result.scalars = MagicMock(return_value=all_settings_scalars)

    # Order: two settings reads, the customer read, then the two tail settings
    # reads (enable_locust_autoscale → "false", autobalance_users_multiplier),
    # then get_all_settings for the [runtime] section.
    # The scheduler-jobs + locust-config service calls are monkeypatched, so
    # they don't consume from this queue.
    db.execute = AsyncMock(
        side_effect=[
            settings_result,   # agent_image_tag
            settings_result,   # ocr_service_url
            settings_result,   # bot_market_data_url (#110)
            customers_result,
            autoscale_off,     # enable_locust_autoscale
            settings_result,   # autobalance_users_multiplier
            all_settings_result,  # get_all_settings (bot [runtime])
        ]
    )
    db.get = AsyncMock(return_value=_fake_server())
    return db


def _patch_phase5_services(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scheduler_jobs: list[SimpleNamespace],
    locust_config: SimpleNamespace | None,
) -> None:
    """Stub out the lazy-imported Phase-5 service entry points.

    :func:`_build_render_context` does:

        from app.services import locust_configs as services_locust
        from app.services import scheduler_jobs as services_scheduler
        ...
        scheduler_db_rows = await services_scheduler.list_jobs(db, stack_id=...)
        locust_db = await services_locust.get_locust_config(db, stack.id)

    We monkeypatch the attribute on the actual module object so the lazy
    import sees our stub regardless of import ordering.
    """
    from app.services import locust_configs as services_locust
    from app.services import scheduler_jobs as services_scheduler

    async def _fake_list_jobs(_db, *, stack_id):
        return scheduler_jobs

    async def _fake_get_locust(_db, _stack_id):
        return locust_config

    monkeypatch.setattr(services_scheduler, "list_jobs", _fake_list_jobs)
    monkeypatch.setattr(
        services_locust, "get_locust_config", _fake_get_locust
    )


# ---------------------------------------------------------------------------
# 1. test_scheduler_render_emits_real_jobs
# ---------------------------------------------------------------------------


async def test_scheduler_render_emits_real_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two scheduler_jobs rows → render_scheduler_config emits both.

    Pins:

    * Both job names + times + commands round-trip through the renderer.
    * Top-level ``"enabled"`` is ``True`` (always — it controls the bot's
      poll cadence, not which jobs run).
    * Each job's ``"enabled"`` flag is preserved per-row (mixed True/False
      stays mixed in the output).
    """
    stack = _fake_stack()
    db = _make_db()
    jobs = [
        _fake_scheduler_job(
            name="cache_warmup",
            time_str="08:30:00",
            enabled=True,
            command="python -m SellerMarket.cache_warmup",
        ),
        _fake_scheduler_job(
            name="run_trading",
            time_str="09:00:00",
            enabled=False,
            command="python -m SellerMarket.run_trading",
        ),
    ]
    _patch_phase5_services(
        monkeypatch, scheduler_jobs=jobs, locust_config=None
    )

    ctx = await stacks_svc._build_render_context(db, stack)
    out = render_scheduler_config(ctx)
    payload = json.loads(out)

    # Top-level is always True (poll-cadence control, not a kill-switch).
    assert payload["enabled"] is True
    # Both jobs present in DB order.
    assert len(payload["jobs"]) == 2
    assert payload["jobs"][0] == {
        "name": "cache_warmup",
        "time": "08:30:00",
        "command": "python -m SellerMarket.cache_warmup",
        "enabled": True,
    }
    assert payload["jobs"][1] == {
        "name": "run_trading",
        "time": "09:00:00",
        "command": "python -m SellerMarket.run_trading",
        "enabled": False,
    }


# ---------------------------------------------------------------------------
# 2. test_scheduler_render_keeps_top_level_enabled_even_when_all_jobs_disabled
# ---------------------------------------------------------------------------


async def test_scheduler_render_keeps_top_level_enabled_even_when_all_jobs_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every job disabled → top-level ``"enabled"`` is STILL True.

    The top-level flag controls the bot's poll cadence (1s vs 60s — see
    ``SellerMarket/scheduler.py:252``), not whether any job fires. The
    per-job ``enabled`` is the real on/off toggle. We keep the top-level
    True so a user re-enabling a job sees it fire within ~1s instead of
    waiting up to 60s for the slow loop to wake up — matching the
    convention in the original ``SellerMarket/scheduler_config.json``.
    """
    stack = _fake_stack()
    db = _make_db()
    jobs = [
        _fake_scheduler_job(
            name="cache_warmup",
            time_str="08:30:00",
            enabled=False,
            command="python -m SellerMarket.cache_warmup",
        ),
        _fake_scheduler_job(
            name="run_trading",
            time_str="09:00:00",
            enabled=False,
            command="python -m SellerMarket.run_trading",
        ),
    ]
    _patch_phase5_services(
        monkeypatch, scheduler_jobs=jobs, locust_config=None
    )

    ctx = await stacks_svc._build_render_context(db, stack)
    out = render_scheduler_config(ctx)
    payload = json.loads(out)

    assert payload["enabled"] is True
    # Per-job enabled is what's actually False; bot's should_run_job will
    # skip these but the top-level fast-poll keeps re-checking the file
    # so a re-enable is picked up within 1s.
    assert len(payload["jobs"]) == 2
    assert all(job["enabled"] is False for job in payload["jobs"])


# ---------------------------------------------------------------------------
# 3. test_locust_render_emits_custom_values
# ---------------------------------------------------------------------------


async def test_locust_render_emits_custom_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted LocustConfig row wins over the fleet defaults.

    The renderer applies each field one-for-one when an override row is
    present; we verify the output JSON matches the row's values exactly,
    not the defaults.
    """
    stack = _fake_stack()
    db = _make_db()
    locust = _fake_locust_config(
        users=42,
        spawn_rate=7,
        run_time="600s",
        host="https://override.example",
        processes=3,
    )
    _patch_phase5_services(
        monkeypatch, scheduler_jobs=[], locust_config=locust
    )

    ctx = await stacks_svc._build_render_context(db, stack)
    out = render_locust_config(ctx)
    payload = json.loads(out)

    assert payload == {
        "locust": {
            "users": 42,
            "spawn_rate": 7,
            "run_time": "600s",
            "host": "https://override.example",
            "processes": 3,
        }
    }


# ---------------------------------------------------------------------------
# 4. test_locust_render_falls_back_to_defaults_when_no_row
# ---------------------------------------------------------------------------


async def test_locust_render_falls_back_to_defaults_when_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No LocustConfig row → documented fleet defaults.

    The defaults are documented at the dataclass level
    (:class:`app.services.rendering.LocustConfigRow`) and re-stated in the
    renderer's ``_DEFAULTS`` mapping. This test pins both ends — if either
    drifts, the test fails loudly.
    """
    stack = _fake_stack()
    db = _make_db()
    _patch_phase5_services(
        monkeypatch, scheduler_jobs=[], locust_config=None
    )

    ctx = await stacks_svc._build_render_context(db, stack)
    out = render_locust_config(ctx)
    payload = json.loads(out)

    assert payload == {
        "locust": {
            "users": 10,
            "spawn_rate": 10,
            "run_time": "120s",
            "host": "https://abc.com",
            "processes": 1,
        }
    }
