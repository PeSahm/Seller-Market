"""Unit tests for the scheduler-job service and schema (Phase 5).

Pure logic tests — no live DB, no SSH. The service-layer helpers are
exercised against mocked sessions; the schema validators run inline through
pydantic.

The interesting branches are:

* the time-string validator (``HH:MM:SS`` exact format),
* the command whitelist membership check,
* the optimistic-lock version compare in :func:`upsert_job`,
* the three "will this re-fire today?" cases driven by old / new / now.

Nothing in this module imports ``app.services.ssh``, ``app.routers``, or
the rendering layer — keeping the unit-test surface narrow keeps test
collection fast and protects against the schema/service split being
accidentally widened in a future revision.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time as time_type
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.schemas.scheduler import (
    ALLOWED_COMMANDS,
    SchedulerJobUpsert,
    is_command_allowed,
)
from app.services import scheduler_jobs as scheduler_svc
from app.services.scheduler_jobs import (
    OptimisticLockError,
    upsert_job,
    will_refire_today,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_upsert_kwargs() -> dict:
    """Minimal valid kwargs for ``SchedulerJobUpsert`` — tests vary one field."""
    return {
        "time": "08:15:30",
        "enabled": True,
        "command": None,
        "version": 1,
    }


# ---------------------------------------------------------------------------
# 1. Time validator
# ---------------------------------------------------------------------------


def test_time_validator_accepts_valid() -> None:
    """``HH:MM:SS`` 24-hour passes; common bad shapes are rejected.

    The four cases:

    * ``"08:15:30"`` — canonical, accepted.
    * ``"08:15"`` — missing seconds; would silently drift away from the
      bot's scheduler key (``f"{name}_{date}_{time_str}"`` includes the
      seconds verbatim).
    * ``"24:00:00"`` — 24 is out of range; midnight is ``"00:00:00"``.
    * ``"abc"`` — not even close.
    """
    # Good.
    kwargs = _base_upsert_kwargs()
    kwargs["time"] = "08:15:30"
    model = SchedulerJobUpsert(**kwargs)
    assert model.time == "08:15:30"

    # Missing seconds.
    bad = _base_upsert_kwargs()
    bad["time"] = "08:15"
    with pytest.raises(ValidationError):
        SchedulerJobUpsert(**bad)

    # Out-of-range hour.
    bad = _base_upsert_kwargs()
    bad["time"] = "24:00:00"
    with pytest.raises(ValidationError):
        SchedulerJobUpsert(**bad)

    # Garbage.
    bad = _base_upsert_kwargs()
    bad["time"] = "abc"
    with pytest.raises(ValidationError):
        SchedulerJobUpsert(**bad)


def test_time_validator_accepts_midnight_and_max() -> None:
    """Boundary cases at the edges of the 24-hour clock.

    The regex matches ``00:00:00`` through ``23:59:59``; we pin both ends
    so a future "let's accept fractional seconds" tweak doesn't break the
    bottom of the range.
    """
    kwargs = _base_upsert_kwargs()

    kwargs["time"] = "00:00:00"
    SchedulerJobUpsert(**kwargs)

    kwargs["time"] = "23:59:59"
    SchedulerJobUpsert(**kwargs)


# ---------------------------------------------------------------------------
# 2. Command whitelist
# ---------------------------------------------------------------------------


def test_command_whitelist() -> None:
    """:func:`is_command_allowed` enforces an exact-string match.

    Substring matches don't count — ``"python cache_warmup.py --debug"``
    is rejected because the bot doesn't recognise the flag and would error
    out anyway. ``rm -rf /`` is the canonical "would-be catastrophic" case
    that must be rejected.

    Also confirms the lookup is per-job-name: a valid ``run_trading``
    command must not be accepted under the ``cache_warmup`` key.
    """
    # Canonical happy path.
    assert is_command_allowed("cache_warmup", "python cache_warmup.py") is True
    assert (
        is_command_allowed(
            "run_trading", "locust -f locustfile_new.py --headless"
        )
        is True
    )

    # Catastrophic command — must be rejected.
    assert is_command_allowed("cache_warmup", "rm -rf /") is False

    # Cross-name confusion — must be rejected.
    assert (
        is_command_allowed(
            "cache_warmup", "locust -f locustfile_new.py --headless"
        )
        is False
    )

    # Unknown name — must return False rather than raising so callers can
    # chain into validation without try/except.
    assert is_command_allowed("rogue_job", "python cache_warmup.py") is False


def test_allowed_commands_structure_is_tuple_of_strings() -> None:
    """:data:`ALLOWED_COMMANDS` shape: ``dict[str, tuple[str, ...]]``.

    The service falls back to ``ALLOWED_COMMANDS[name][0]`` when the form
    omits ``command``, so the inner type MUST be index-able. A list would
    also work but the constant declaration uses ``tuple`` for immutability
    — a stray ``.append`` in a future test would otherwise silently widen
    the whitelist for the rest of the run.
    """
    assert set(ALLOWED_COMMANDS) == {"cache_warmup", "run_trading"}
    for name, commands in ALLOWED_COMMANDS.items():
        assert isinstance(commands, tuple), (
            f"ALLOWED_COMMANDS[{name!r}] must be a tuple"
        )
        assert all(isinstance(c, str) for c in commands)
        assert len(commands) >= 1


# ---------------------------------------------------------------------------
# 3. Optimistic-lock branch in upsert_job
# ---------------------------------------------------------------------------


async def test_optimistic_lock_version_mismatch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``upsert_job`` raises ``OptimisticLockError`` on version mismatch.

    We stub :func:`app.services.scheduler_jobs.get_job` so the test doesn't
    need a real SQLAlchemy session — the only branch we want to exercise is
    the version compare. ``db.flush`` and ``db.commit`` must NOT be called
    on the mismatch path, so we assert that too.
    """
    db = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()

    fake_row = SimpleNamespace(
        id=uuid.uuid4(),
        stack_id=uuid.uuid4(),
        name="cache_warmup",
        time=time_type(8, 0, 0),
        enabled=True,
        command="python cache_warmup.py",
        version=5,
    )

    async def _fake_get(_db, _stack, _name):
        return fake_row

    monkeypatch.setattr(scheduler_svc, "get_job", _fake_get)

    upsert = SchedulerJobUpsert(
        time="09:00:00", enabled=True, command=None, version=4
    )

    with pytest.raises(OptimisticLockError):
        await upsert_job(
            db,
            fake_row.stack_id,
            "cache_warmup",
            upsert,
            actor_id=uuid.uuid4(),
        )

    # Critical: nothing should have been written on the mismatch path.
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


async def test_upsert_rejects_unknown_job_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service layer rejects job names not in :data:`ALLOWED_COMMANDS`.

    The schema's ``Literal`` already enforces this at the HTTP boundary,
    but the service-level check defends against bypasses (e.g. a future
    bulk-import script that constructs the payload directly).
    """
    db = MagicMock()
    upsert = SchedulerJobUpsert(
        time="08:00:00", enabled=True, command=None, version=0
    )
    with pytest.raises(ValueError, match="unknown scheduler job name"):
        await upsert_job(
            db, uuid.uuid4(), "rogue_job", upsert, actor_id=uuid.uuid4()
        )


async def test_upsert_rejects_command_outside_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit non-whitelisted ``command`` must be rejected.

    The default-fallback path (``command=None``) is exercised via the
    other tests; this one pins the explicit-command branch.
    """
    db = MagicMock()
    upsert = SchedulerJobUpsert(
        time="08:00:00",
        enabled=True,
        command="rm -rf /",
        version=0,
    )
    with pytest.raises(ValueError, match="command not in whitelist"):
        await upsert_job(
            db,
            uuid.uuid4(),
            "cache_warmup",
            upsert,
            actor_id=uuid.uuid4(),
        )


# ---------------------------------------------------------------------------
# 4-6. will_refire_today logic
# ---------------------------------------------------------------------------


async def test_will_refire_today_in_future_after_already_past(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old time fired (08:00 < now=08:30), new time is future (09:00) → re-fire.

    This is the dangerous case the banner exists to warn about: the bot
    already fired the 08:00 run today, the operator pushes the schedule to
    09:00, the dedupe key changes, and at 09:00 the bot fires again.
    """
    fake_row = SimpleNamespace(
        id=uuid.uuid4(),
        stack_id=uuid.uuid4(),
        name="cache_warmup",
        time=time_type(8, 0, 0),
        enabled=True,
        command="python cache_warmup.py",
        version=1,
    )

    async def _fake_get(_db, _stack, _name):
        return fake_row

    monkeypatch.setattr(scheduler_svc, "get_job", _fake_get)

    result = await will_refire_today(
        MagicMock(),
        fake_row.stack_id,
        "cache_warmup",
        "09:00:00",
        now_local=datetime(2026, 5, 16, 8, 30, 0),
    )

    assert result.will_refire is True
    assert "re-fire" in result.reason.lower()


async def test_will_refire_today_new_time_in_past(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old 08:00 fired, new time 07:00 also already past at 08:30 → no re-fire.

    Both timestamps are behind ``now``. Even though the dedupe key
    changes, the bot only fires when the wall clock matches; that moment
    has gone by, so no second fire today.
    """
    fake_row = SimpleNamespace(
        id=uuid.uuid4(),
        stack_id=uuid.uuid4(),
        name="cache_warmup",
        time=time_type(8, 0, 0),
        enabled=True,
        command="python cache_warmup.py",
        version=1,
    )

    async def _fake_get(_db, _stack, _name):
        return fake_row

    monkeypatch.setattr(scheduler_svc, "get_job", _fake_get)

    result = await will_refire_today(
        MagicMock(),
        fake_row.stack_id,
        "cache_warmup",
        "07:00:00",
        now_local=datetime(2026, 5, 16, 8, 30, 0),
    )

    assert result.will_refire is False
    assert "past" in result.reason.lower()


async def test_will_refire_today_old_time_still_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old 09:00 hasn't fired yet at now=08:30 → no re-fire risk.

    The bot hasn't fired today at all (the trigger is still in the
    future), so changing the time can't cause a duplicate — the next fire
    is just the first fire, on the new schedule.
    """
    fake_row = SimpleNamespace(
        id=uuid.uuid4(),
        stack_id=uuid.uuid4(),
        name="cache_warmup",
        time=time_type(9, 0, 0),
        enabled=True,
        command="python cache_warmup.py",
        version=1,
    )

    async def _fake_get(_db, _stack, _name):
        return fake_row

    monkeypatch.setattr(scheduler_svc, "get_job", _fake_get)

    result = await will_refire_today(
        MagicMock(),
        fake_row.stack_id,
        "cache_warmup",
        "09:30:00",
        now_local=datetime(2026, 5, 16, 8, 30, 0),
    )

    assert result.will_refire is False
    assert "future" in result.reason.lower()


async def test_will_refire_today_no_existing_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first-time create has no "old time" — heuristic returns False.

    The banner copy is general enough that we'd rather not show a warning
    on a brand-new job; the route can show a different message for the
    create case.
    """

    async def _fake_get(_db, _stack, _name):
        return None

    monkeypatch.setattr(scheduler_svc, "get_job", _fake_get)

    result = await will_refire_today(
        MagicMock(),
        uuid.uuid4(),
        "cache_warmup",
        "09:00:00",
        now_local=datetime(2026, 5, 16, 8, 30, 0),
    )

    assert result.will_refire is False
    assert "first-time" in result.reason.lower()
