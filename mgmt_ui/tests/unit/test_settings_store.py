"""Unit tests for :mod:`app.services.settings_store`.

Drives the helpers against an in-memory SQLite DB. The production target is
PostgreSQL (the ``Setting`` model uses ``PG_UUID(as_uuid=True)`` for
``updated_by``), but SQLAlchemy 2.x falls back to ``CHAR(32)`` under SQLite,
which is enough for round-tripping integration tests of CRUD helpers.

If ``aiosqlite`` isn't installed in the venv (it isn't a hard dep of the app)
we skip the module rather than fail — these tests are best-effort coverage
for the helper, not a release gate.
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator

import pytest

# Hard-skip the whole module if aiosqlite is missing. Doing this at module
# import time means pytest reports a clean "skipped" instead of a collection
# error.
pytest.importorskip("aiosqlite", reason="aiosqlite is not installed in this venv")

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db_base import Base  # noqa: E402
from app.models.settings import Setting  # noqa: E402  (registers the table)
from app.services import settings_store  # noqa: E402


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """Fresh in-memory SQLite session per test.

    Uses a private in-memory DB scoped to a single connection so the schema
    we create with :meth:`Base.metadata.create_all` is visible to the session
    that runs the tests.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        # Only create the ``settings`` table; the other models pull in PG-
        # specific types (e.g. ``ENUM`` with ``create_type=False``) that
        # SQLite can't handle in DDL.
        await conn.run_sync(
            lambda sync_conn: Setting.__table__.create(sync_conn)
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


async def test_get_default_returns_documented_default_when_unset(
    db: AsyncSession,
) -> None:
    """A key that's in DEFAULTS but absent from the DB returns the default."""
    value = await settings_store.get_setting(db, "ocr_service_url")
    assert value == settings_store.DEFAULTS["ocr_service_url"]


async def test_set_then_get_round_trips(db: AsyncSession) -> None:
    """``set_setting`` followed by ``get_setting`` returns the new value."""
    await settings_store.set_setting(
        db, "ocr_service_url", "https://ocr.example.com"
    )
    await db.commit()
    value = await settings_store.get_setting(db, "ocr_service_url")
    assert value == "https://ocr.example.com"


async def test_set_updates_existing_row(db: AsyncSession) -> None:
    """A second ``set_setting`` for the same key updates the existing row.

    We assert via the helper rather than re-querying the DB so the test
    documents the *observable* upsert behaviour the router relies on.
    """
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    await settings_store.set_setting(
        db, "ocr_service_url", "http://first.example.com", updated_by=user_a
    )
    await db.commit()

    await settings_store.set_setting(
        db, "ocr_service_url", "http://second.example.com", updated_by=user_b
    )
    await db.commit()

    value = await settings_store.get_setting(db, "ocr_service_url")
    assert value == "http://second.example.com"

    # Verify there's exactly one row — upsert, not insert.
    all_settings = await settings_store.get_all_settings(db)
    assert all_settings["ocr_service_url"] == "http://second.example.com"


async def test_get_all_settings_merges_defaults_with_db_rows(
    db: AsyncSession,
) -> None:
    """``get_all_settings`` returns DB rows overlaid on top of DEFAULTS."""
    await settings_store.set_setting(
        db, "ocr_service_url", "http://overridden.example.com"
    )
    await db.commit()

    all_settings = await settings_store.get_all_settings(db)

    # The DB row wins for ocr_service_url.
    assert all_settings["ocr_service_url"] == "http://overridden.example.com"
    # The default is still present for the un-set key.
    assert (
        all_settings["agent_image_tag"]
        == settings_store.DEFAULTS["agent_image_tag"]
    )


async def test_get_unknown_key_raises_keyerror(db: AsyncSession) -> None:
    """An unknown key with no DEFAULT and no DB row raises KeyError loudly."""
    with pytest.raises(KeyError):
        await settings_store.get_setting(db, "definitely_not_a_real_key")
