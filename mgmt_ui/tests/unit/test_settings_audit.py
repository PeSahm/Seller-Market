"""Unit tests for the Phase 9 audit-log backfill in ``set_setting``.

Every call to :func:`app.services.settings_store.set_setting` MUST emit a
matching ``audit_log`` row (``action="setting.update"``, ``target_type=
"setting"``, ``target_id=<key>``) alongside the upsert. The audit row is
written even when the new value is identical to the previous one — the
existing pattern across the codebase is "admins can always see that this
update happened" (the diff just surfaces as empty in that case). For a
brand-new key the ``before_json`` value is ``None``, which renders in
the diff view as an "added" entry.

We pin three behaviours here:

* New key: one ``setting.update`` audit row, ``before_json["value"]`` is
  ``None``, ``after_json["value"]`` is the new value, ``target_id`` is
  the key string (not a UUID).
* Existing key whose value changed: ``before_json["value"]`` is the
  previous value, ``after_json["value"]`` is the new value.
* Existing key with an unchanged value: an audit row IS still emitted
  (matches the "admins can always see the update happened" pattern).

DB is mocked end-to-end — these tests are pure unit coverage of the
write-path logic, not a round-trip against a real Postgres.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.audit import AuditLog
from app.models.settings import Setting
from app.services import settings_store


def _make_mock_db(existing_row: object | None) -> MagicMock:
    """Build an ``AsyncSession`` stand-in that returns ``existing_row``.

    ``db.execute`` returns a fake result whose ``scalar_one_or_none()``
    hands back ``existing_row`` (``None`` -> "key doesn't exist yet";
    a row -> "key already exists in the DB"). ``db.add`` is a regular
    ``MagicMock`` so the tests can inspect everything that got staged
    (both the audit row and the upserted ``Setting``).
    """
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=existing_row)
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    return db


def _added_audit_rows(db: MagicMock) -> list[AuditLog]:
    """Pull every :class:`AuditLog` row that was staged on ``db.add``."""
    return [
        call.args[0]
        for call in db.add.call_args_list
        if call.args and isinstance(call.args[0], AuditLog)
    ]


@pytest.mark.asyncio
async def test_set_setting_emits_audit_for_new_key() -> None:
    """A brand-new key emits ``setting.update`` with ``before.value=None``.

    Also asserts the ``target_id`` is the key string itself, not a UUID
    — the spec for Phase 9 explicitly calls this out because the rest
    of the audit log uses stringified UUIDs.
    """
    db = _make_mock_db(existing_row=None)
    actor = uuid.uuid4()

    await settings_store.set_setting(
        db, "ocr_service_url", "http://new.example.com", updated_by=actor
    )

    audits = _added_audit_rows(db)
    assert len(audits) == 1
    row = audits[0]
    assert row.action == "setting.update"
    assert row.target_type == "setting"
    # target_id is the key STRING, not a UUID. The audit_log.target_id
    # column is Text, not a UUID type, precisely so non-UUID identifiers
    # like setting keys are first-class.
    assert row.target_id == "ocr_service_url"
    assert isinstance(row.target_id, str)
    # before_json[value] is None for a brand-new key — diff_json renders
    # this as an "added" entry in the UI.
    assert row.before_json == {"value": None}
    assert row.after_json == {"value": "http://new.example.com"}
    assert row.actor_user_id == actor


@pytest.mark.asyncio
async def test_set_setting_emits_audit_when_value_unchanged() -> None:
    """A no-op write (new value == previous value) STILL emits the audit row.

    Matches the existing pattern in the codebase: admins can always see
    that a write happened, even when the diff is empty. The
    audit-detail UI just renders a zero-entry diff table in that case.
    """
    db = _make_mock_db(
        existing_row=SimpleNamespace(
            key="agent_image_tag",
            value="ghcr.io/pesahm/seller-market:latest",
            updated_by=None,
            updated_at=None,
        )
    )
    actor = uuid.uuid4()

    await settings_store.set_setting(
        db,
        "agent_image_tag",
        "ghcr.io/pesahm/seller-market:latest",  # same as existing
        updated_by=actor,
    )

    audits = _added_audit_rows(db)
    assert len(audits) == 1, (
        "audit row must be emitted even when the value didn't actually change"
    )
    row = audits[0]
    assert row.action == "setting.update"
    assert row.target_type == "setting"
    assert row.target_id == "agent_image_tag"
    # Before and after carry the same value — the diff view will render
    # this as an empty changes table, which is the expected UX.
    assert row.before_json == {"value": "ghcr.io/pesahm/seller-market:latest"}
    assert row.after_json == {"value": "ghcr.io/pesahm/seller-market:latest"}
    assert row.actor_user_id == actor


@pytest.mark.asyncio
async def test_set_setting_audit_captures_previous_value() -> None:
    """When an existing key is updated, before.value is the OLD value.

    Pins the read-before-write ordering inside ``set_setting``: the
    audit row must capture the row's current ``value`` *before* the
    upsert assigns the new one, otherwise both panels would show the
    new value and the diff would always be empty.
    """
    db = _make_mock_db(
        existing_row=SimpleNamespace(
            key="ocr_service_url",
            value="http://old.example.com",
            updated_by=None,
            updated_at=None,
        )
    )

    await settings_store.set_setting(
        db, "ocr_service_url", "http://new.example.com", updated_by=None
    )

    audits = _added_audit_rows(db)
    assert len(audits) == 1
    row = audits[0]
    assert row.before_json == {"value": "http://old.example.com"}
    assert row.after_json == {"value": "http://new.example.com"}
    # actor_user_id is nullable — the audit row's actor mirrors whatever
    # the caller passed, which may be None for system-triggered writes.
    assert row.actor_user_id is None


@pytest.mark.asyncio
async def test_set_setting_audit_actor_id_omitted_uses_none() -> None:
    """Audit row's actor is ``None`` when ``updated_by`` isn't passed.

    The kwarg defaults to ``None`` for both ``Setting.updated_by`` and
    the audit row's ``actor_user_id`` — important so a script or
    migration that calls ``set_setting`` without a user context
    doesn't crash on a NOT-NULL violation.
    """
    db = _make_mock_db(existing_row=None)

    await settings_store.set_setting(
        db, "agent_locust_processes_cap", "8"
    )

    audits = _added_audit_rows(db)
    assert len(audits) == 1
    assert audits[0].actor_user_id is None
    # Sanity-check the upserted Setting row also went through.
    settings_rows = [
        call.args[0]
        for call in db.add.call_args_list
        if call.args and isinstance(call.args[0], Setting)
    ]
    assert len(settings_rows) == 1
    assert settings_rows[0].key == "agent_locust_processes_cap"
    assert settings_rows[0].value == "8"
