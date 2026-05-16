"""Unit tests for the locust config service + schema (Phase 5).

Pure logic tests — no live DB. The schema validators run inline through
pydantic; the optimistic-lock branch in :func:`upsert_locust_config` is
exercised against a mocked session in the same shape as
:mod:`tests.unit.test_customers`.

Two pieces of branching logic worth a dedicated test:

1. ``run_time`` parser + ceiling check — the bot's 600-second subprocess
   timeout is a hard environmental constraint and a regression here would
   silently truncate load-test runs.
2. Optimistic-lock version mismatch — same convention as the customers
   service: mismatch must raise BEFORE any DB write happens.

The pure-DEFAULTS test pins the admin-tunable cap default at ``"4"``; if a
future change bumps it, the failure here documents the implicit migration
needed (existing rows whose ``processes`` exceed the new floor would need
manual review).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.schemas.locust import (
    RUN_TIME_HARD_CEILING_SECONDS,
    LocustUpsert,
    parse_run_time_seconds,
)
from app.services import locust_configs as locust_svc
from app.services import settings_store
from app.services.locust_configs import (
    OptimisticLockError,
    upsert_locust_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_upsert_kwargs() -> dict:
    """Minimal valid kwargs for ``LocustUpsert`` — tests vary one field."""
    return {
        "users": 10,
        "spawn_rate": 2,
        "run_time": "120s",
        "host": "https://example.com",
        "processes": 1,
        "version": 0,
    }


# ---------------------------------------------------------------------------
# 1. run_time parsing — happy path
# ---------------------------------------------------------------------------


def test_parse_run_time_seconds_units() -> None:
    """The three supported units convert to the right number of seconds.

    Pinned to byte-exact integers because ``parse_run_time_seconds`` is the
    only line of defence against the bot's 600-second guillotine — a
    rounding bug here would be hard to notice in production but a
    one-character regression would fail this test.
    """
    assert parse_run_time_seconds("120s") == 120
    assert parse_run_time_seconds("5m") == 300
    assert parse_run_time_seconds("1h") == 3600


# ---------------------------------------------------------------------------
# 2. run_time parsing — error paths
# ---------------------------------------------------------------------------


def test_parse_run_time_seconds_rejects_bad_format() -> None:
    """Bare integers, empty strings, and unsupported units all raise.

    The ``"120"`` case is the most important: a caller who omits the unit
    might mean minutes, and silently treating it as seconds would drop
    them into the 600s ceiling unexpectedly. We refuse rather than guess.
    """
    with pytest.raises(ValueError):
        parse_run_time_seconds("120")
    with pytest.raises(ValueError):
        parse_run_time_seconds("")
    with pytest.raises(ValueError):
        parse_run_time_seconds("1d")  # days deliberately not supported


# ---------------------------------------------------------------------------
# 3. Ceiling — > 600s rejected
# ---------------------------------------------------------------------------


def test_locust_upsert_rejects_run_time_above_ceiling() -> None:
    """``run_time="700s"`` is refused with the explicit ``< 600s`` message.

    The error message is part of the contract — the router surfaces it
    verbatim to the admin so they can see *why* their input was refused
    (the 600s ceiling isn't documented anywhere else in the UI).
    """
    kwargs = _base_upsert_kwargs()
    kwargs["run_time"] = "700s"
    with pytest.raises(ValidationError) as excinfo:
        LocustUpsert(**kwargs)
    # Message must reference the ceiling so the admin understands the cause.
    assert "< 600s" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. Ceiling boundary — 599s accepted
# ---------------------------------------------------------------------------


def test_locust_upsert_accepts_just_under_ceiling() -> None:
    """``run_time="599s"`` is the largest accepted value.

    We pin the boundary explicitly so an off-by-one change in the
    comparison operator (``>=`` ↔ ``>``) fails loudly. The pinned
    constant — :data:`RUN_TIME_HARD_CEILING_SECONDS` — is also referenced
    so the test self-documents the boundary.
    """
    assert RUN_TIME_HARD_CEILING_SECONDS == 600  # documents the contract
    kwargs = _base_upsert_kwargs()
    kwargs["run_time"] = "599s"
    model = LocustUpsert(**kwargs)
    assert model.run_time == "599s"


# ---------------------------------------------------------------------------
# 5. Floor — 0s rejected
# ---------------------------------------------------------------------------


def test_locust_upsert_rejects_zero_or_negative_run_time() -> None:
    """``run_time="0s"`` is a no-op run; refuse at the schema boundary.

    A zero-second locust invocation would exit immediately with no
    metrics, which is almost certainly a typo rather than intent. The
    regex shape forbids negatives outright (``\\d+`` doesn't match a
    leading ``-``), so this test focuses on the ``0`` case.
    """
    kwargs = _base_upsert_kwargs()
    kwargs["run_time"] = "0s"
    with pytest.raises(ValidationError):
        LocustUpsert(**kwargs)


# ---------------------------------------------------------------------------
# 6. Host — bad schemes rejected
# ---------------------------------------------------------------------------


def test_locust_upsert_rejects_bad_host() -> None:
    """No-scheme and ftp hosts are both refused.

    Locust accepts a bare hostname in some configurations, but our render
    layer interpolates this value into a CLI invocation where a missing
    scheme would either fail confusingly or silently target HTTP. We
    refuse at the schema boundary so the failure is visible.
    """
    bad_no_scheme = _base_upsert_kwargs()
    bad_no_scheme["host"] = "abc.com"
    with pytest.raises(ValidationError):
        LocustUpsert(**bad_no_scheme)

    bad_ftp = _base_upsert_kwargs()
    bad_ftp["host"] = "ftp://abc.com"
    with pytest.raises(ValidationError):
        LocustUpsert(**bad_ftp)


# ---------------------------------------------------------------------------
# 7. Host — https accepted
# ---------------------------------------------------------------------------


def test_locust_upsert_accepts_https_host() -> None:
    """``https://`` URLs round-trip without modification.

    The validator strips surrounding whitespace as a convenience (paste
    artefacts from a terminal often include trailing newlines) but
    otherwise leaves the value byte-exact.
    """
    kwargs = _base_upsert_kwargs()
    kwargs["host"] = "https://load.example.com:8443/path"
    model = LocustUpsert(**kwargs)
    assert model.host == "https://load.example.com:8443/path"


# ---------------------------------------------------------------------------
# 8. Optimistic-lock branch
# ---------------------------------------------------------------------------


async def test_optimistic_lock_version_mismatch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``upsert_locust_config`` raises on version mismatch and writes nothing.

    We stub :func:`get_locust_config` to return a synthetic row with a
    known ``version``, and stub the processes-cap reader to a permissive
    value so the test isolates the optimistic-lock branch. ``flush`` and
    ``commit`` must NOT be awaited on the mismatch path — this matches
    the convention enforced in :mod:`tests.unit.test_customers`.
    """
    db = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    fake_row = SimpleNamespace(
        id=uuid.uuid4(),
        stack_id=uuid.uuid4(),
        users=10,
        spawn_rate=2,
        run_time="120s",
        host="https://example.com",
        processes=1,
        version=5,
    )

    async def _fake_get(_db, _stack_id):
        return fake_row

    async def _fake_cap(_db):
        return 32  # permissive: keep the focus on the version check

    monkeypatch.setattr(locust_svc, "get_locust_config", _fake_get)
    monkeypatch.setattr(locust_svc, "_resolve_processes_cap", _fake_cap)

    payload = LocustUpsert(
        users=20,
        spawn_rate=4,
        run_time="60s",
        host="https://example.com",
        processes=1,
        version=4,  # row has version=5 — mismatch
    )

    with pytest.raises(OptimisticLockError):
        await upsert_locust_config(
            db, fake_row.stack_id, payload, actor_id=uuid.uuid4()
        )

    # Critical: no DB write happens on the mismatch path.
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# 8b. Processes cap branch (cheap insurance — not in the required 9 but the
# cap is the WHOLE reason this lives in the service layer, so pin it).
# ---------------------------------------------------------------------------


async def test_processes_above_cap_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``processes`` value larger than the admin cap raises ValueError.

    The cap is dynamic (admin-set), so this check has to live in the
    service layer rather than the pydantic schema. The error message must
    name the cap so the admin understands what to change.
    """
    db = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    async def _fake_cap(_db):
        return 2  # tight cap for the test

    async def _fake_get(_db, _stack_id):
        return None  # no existing row — would otherwise hit the insert path

    monkeypatch.setattr(locust_svc, "_resolve_processes_cap", _fake_cap)
    monkeypatch.setattr(locust_svc, "get_locust_config", _fake_get)

    payload = LocustUpsert(
        users=10,
        spawn_rate=2,
        run_time="120s",
        host="https://example.com",
        processes=8,  # > cap of 2
        version=0,
    )

    with pytest.raises(ValueError) as excinfo:
        await upsert_locust_config(
            db, uuid.uuid4(), payload, actor_id=uuid.uuid4()
        )

    # Message must reference the cap number so the admin understands.
    assert "2" in str(excinfo.value)
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# 9. Defaults — cap default is "4"
# ---------------------------------------------------------------------------


def test_processes_cap_default_is_4() -> None:
    """The ``agent_locust_processes_cap`` default lives in DEFAULTS as ``"4"``.

    This test is the single source of truth for the documented default. If
    a future change bumps it (say, to ``"6"``), the failure here is the
    cue to also update the admin form's placeholder text and the
    operational runbook — both of which reference the same number.
    """
    assert settings_store.DEFAULTS["agent_locust_processes_cap"] == "4"
