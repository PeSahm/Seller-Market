"""Regression test for the asyncpg prepared-statement-cache fix.

asyncpg caches each statement's server-side prepared PLAN. When the shared
external DB's schema changes under a live pooled connection — a migration on
deploy from the OTHER mgmt instance, or a failover/restore onto the warm spare
— the cached plan goes stale and the next query raises
``asyncpg.InvalidCachedStatementError`` ("cached statement plan is invalid due
to a database schema or configuration change"). That surfaced as an
intermittent HTTP 500 (observed live on ``POST .../trade-instructions``) which
"self-heals" only after asyncpg evicts its cache.

``statement_cache_size=0`` disables asyncpg's native cache so the error can't
arise; ``prepared_statement_cache_size=0`` mirrors it for SQLAlchemy's own
prepared-statement name cache. This test pins both, and that the single engine
factory applies them to BOTH the main engine and the failover spare (a failover
that rebuilt the spare without the hardening would reintroduce the 500).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import db as db_mod


@pytest.fixture(autouse=True)
def _reset_db():
    db_mod._reset_to_main_for_tests()
    yield
    db_mod._reset_to_main_for_tests()


def test_connect_args_disable_prepared_statement_cache():
    assert db_mod._ASYNCPG_CONNECT_ARGS["statement_cache_size"] == 0
    assert db_mod._ASYNCPG_CONNECT_ARGS["prepared_statement_cache_size"] == 0


def test_build_engine_applies_connect_args(monkeypatch):
    captured = {}

    def _fake_create(dsn, **kw):
        captured["dsn"] = dsn
        captured["kw"] = kw
        return object()

    monkeypatch.setattr(db_mod, "create_async_engine", _fake_create)
    db_mod._build_engine("postgresql+asyncpg://u:p@h:5432/db")

    connect_args = captured["kw"]["connect_args"]
    assert connect_args["statement_cache_size"] == 0
    assert connect_args["prepared_statement_cache_size"] == 0
    assert captured["kw"]["pool_pre_ping"] is True


def test_spare_engine_is_built_with_the_same_hardening(monkeypatch):
    # The failover spare MUST be built through the same factory, else a
    # failover would reintroduce the cached-plan 500 on the standby DB.
    built: list[dict] = []
    monkeypatch.setattr(
        db_mod,
        "create_async_engine",
        lambda dsn, **kw: built.append(kw) or object(),
    )
    monkeypatch.setattr(
        db_mod,
        "get_settings",
        lambda: SimpleNamespace(spare_dsn="postgresql+asyncpg://u:p@spare:5432/db"),
    )
    db_mod.spare_engine = None  # force a lazy rebuild via _build_engine

    assert db_mod.get_spare_engine() is not None
    assert built[-1]["connect_args"]["statement_cache_size"] == 0
    assert built[-1]["connect_args"]["prepared_statement_cache_size"] == 0
