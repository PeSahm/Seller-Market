"""Dedup semantics for the health-signal upsert (Phase 8).

The scanner inserts one row per anomaly hit, but a chronic condition (broker
rate-limited for an hour) can fire the same regex dozens of times per tick.
:func:`app.services.health_signals._upsert_signal` collapses that into a
single row by bumping ``ts`` on an existing unacked row within a 60-minute
window, returning ``("bumped", row)`` instead of ``("inserted", row)``.

Five contract points to pin:

1. First sighting of a (stack, kind) pair inserts a new row.
2. A second sighting within the window bumps the existing row (no insert).
3. A different ``kind`` within the window inserts a fresh row (kinds are
   independent dedup buckets).
4. A second sighting *outside* the 60-minute window inserts a fresh row
   (the older row no longer satisfies the dedup query).
5. A second sighting within the window but where the prior row was already
   acked also inserts a fresh row — once an operator has triaged it,
   recurrence deserves its own row so it doesn't get silently absorbed.

We mock the AsyncSession minimally rather than spin up a real Postgres:
the SQL is one ``SELECT ... LIMIT 1`` plus an optional ``INSERT``, and the
contract we care about is "what does the caller observe", not "what SQL is
emitted". The ``FakeDB`` fixture below answers ``execute()`` from a
test-supplied row (or ``None``) and tracks ``add()`` calls so we can assert
inserts vs. bumps.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


# ---------------------------------------------------------------------------
# Fake AsyncSession
# ---------------------------------------------------------------------------


class _FakeResult:
    """Mimics the bits of ``Result`` ``_upsert_signal`` reads."""

    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeDB:
    """Minimal AsyncSession stand-in.

    ``next_existing`` is what the next ``execute()`` call should return via
    ``scalar_one_or_none()``. Set it before each call. We also expose
    ``added`` (every object passed to :meth:`add`) so tests can assert
    insert vs. bump.
    """

    def __init__(self):
        self.next_existing = None
        self.added: list[object] = []
        self.flush = AsyncMock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def execute(self, stmt):  # noqa: ARG002 — we don't inspect the stmt
        return _FakeResult(self.next_existing)

    def add(self, obj):
        self.added.append(obj)


@pytest.fixture
def db() -> _FakeDB:
    return _FakeDB()


@pytest.fixture
def stack_id():
    return uuid4()


def _existing(stack_id, kind: str, *, age: timedelta, acked: bool = False):
    """Build a fake existing HealthSignal-shaped row.

    Uses :class:`SimpleNamespace` rather than the ORM class so we don't need
    a live mapper / session — ``_upsert_signal`` only touches ``.ts`` on the
    object it gets back from the SELECT.
    """
    return SimpleNamespace(
        id=uuid4(),
        stack_id=stack_id,
        kind=kind,
        severity="warning",
        message="prev",
        raw="prev raw",
        ts=datetime.now(timezone.utc) - age,
        ack_by=uuid4() if acked else None,
        ack_at=datetime.now(timezone.utc) - timedelta(minutes=5) if acked else None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_first_sighting_inserts(db, stack_id):
    """No existing row => insert + db.add called once."""
    from app.services.health_signals import _upsert_signal

    db.next_existing = None
    action, row = await _upsert_signal(
        db,
        stack_id=stack_id,
        kind="broker_rate_limit",
        severity="warning",
        message="Broker returned 429",
        raw="HTTP 429 too many requests",
    )

    assert action == "inserted"
    assert row is db.added[0]
    assert len(db.added) == 1
    # The new row should carry the truncated raw and original kind/severity.
    assert db.added[0].kind == "broker_rate_limit"
    assert db.added[0].severity == "warning"
    assert db.added[0].message == "Broker returned 429"


async def test_second_sighting_within_window_bumps(db, stack_id):
    """Existing unacked row within 60m => bump ts, no insert."""
    from app.services.health_signals import _upsert_signal

    prev = _existing(stack_id, "broker_rate_limit", age=timedelta(minutes=15))
    original_ts = prev.ts
    db.next_existing = prev

    action, row = await _upsert_signal(
        db,
        stack_id=stack_id,
        kind="broker_rate_limit",
        severity="warning",
        message="Broker returned 429",
        raw="HTTP 429 still happening",
    )

    assert action == "bumped"
    assert row is prev
    assert len(db.added) == 0, "no new row should have been added"
    # ts must have moved forward to "now".
    assert prev.ts > original_ts


async def test_different_kind_within_window_inserts(db, stack_id):
    """Different ``kind`` is an independent dedup bucket => insert."""
    from app.services.health_signals import _upsert_signal

    # The SELECT filters on kind, so a recent row for kind=A is invisible
    # to a lookup for kind=B — the fake DB simulates that by returning None
    # for the B-kind query.
    db.next_existing = None

    action, row = await _upsert_signal(
        db,
        stack_id=stack_id,
        kind="captcha_fail",
        severity="warning",
        message="Captcha decode failed",
        raw="captcha failed retry 3",
    )

    assert action == "inserted"
    assert len(db.added) == 1
    assert db.added[0].kind == "captcha_fail"


async def test_same_kind_after_window_inserts(db, stack_id):
    """Existing row > 60m old => SELECT returns None => insert.

    The cutoff filter ``ts > now - 60m`` is applied at the DB layer; our
    fake reflects that by returning ``None`` for the lookup. This tests the
    caller's behaviour given that DB-level filtering, not the cutoff math
    itself (which is a one-liner ``datetime.now() - _DEDUP_WINDOW`` and not
    worth a separate assertion).
    """
    from app.services.health_signals import _upsert_signal

    db.next_existing = None  # SELECT excludes the >60m-old row

    action, row = await _upsert_signal(
        db,
        stack_id=stack_id,
        kind="broker_rate_limit",
        severity="warning",
        message="Broker returned 429",
        raw="HTTP 429",
    )

    assert action == "inserted"
    assert len(db.added) == 1


async def test_acked_existing_does_not_dedup(db, stack_id):
    """Already-acked rows are excluded by the SELECT => fresh insert.

    Once an operator clicks ack, the next occurrence is a *recurrence after
    triage* and must surface as its own row instead of silently bumping the
    acked one (which would erase the "happening again" signal entirely).
    The dedup SELECT enforces ``ack_at IS NULL``, so the DB returns nothing
    even though a row with same (stack, kind) exists within the window.
    """
    from app.services.health_signals import _upsert_signal

    # Acked row exists in the table but is filtered out by ack_at IS NULL,
    # so the SELECT returns None.
    db.next_existing = None

    action, row = await _upsert_signal(
        db,
        stack_id=stack_id,
        kind="broker_rate_limit",
        severity="warning",
        message="Broker returned 429",
        raw="HTTP 429 again after ack",
    )

    assert action == "inserted"
    assert len(db.added) == 1


async def test_raw_truncated_to_2000_chars(db, stack_id):
    """Very long raw lines are truncated from the END to 2000 chars.

    We slice on code points (``raw[-2000:]``) so a 5000-char bot-stacktrace
    line yields exactly 2000 chars on the row — the tail, since the
    interesting error context lives there rather than at the start of a
    verbose header.
    """
    from app.services.health_signals import _upsert_signal

    db.next_existing = None
    raw_long = ("x" * 5000) + "TAIL_MARKER_at_end"

    action, _row = await _upsert_signal(
        db,
        stack_id=stack_id,
        kind="broker_timeout",
        severity="error",
        message="Broker timed out",
        raw=raw_long,
    )

    assert action == "inserted"
    inserted = db.added[0]
    assert inserted.raw is not None
    assert len(inserted.raw) == 2000
    # The TAIL_MARKER lives at the end and must survive truncation.
    assert inserted.raw.endswith("TAIL_MARKER_at_end")
