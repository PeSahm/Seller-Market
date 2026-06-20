"""Tests for app-level DB auto-failover (#156).

The DB engine + network are mocked so the failover logic (rebind, threshold,
no-auto-failback, marker, leader re-election, cron-clobber skip) is exercised
offline.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import db as db_mod
from app.services import db_backup, db_failover


@pytest.fixture(autouse=True)
def _reset_db():
    db_mod._reset_to_main_for_tests()
    yield
    db_mod._reset_to_main_for_tests()


def _app():
    return SimpleNamespace(
        state=SimpleNamespace(is_worker_leader=True, active_db="main", failed_over_at=None)
    )


def _supervisor_settings(marker="/tmp/FAILOVER_ACTIVE"):
    return SimpleNamespace(
        db_probe_interval_seconds=0.01,
        db_probe_failure_threshold=2,
        db_probe_timeout_seconds=0.01,
        resolved_failover_marker_path=lambda: marker,
        enable_worker_leader_election=True,
    )


# --- db.activate_spare (the rebind) ---------------------------------------


@pytest.mark.asyncio
async def test_activate_spare_rebinds(monkeypatch):
    monkeypatch.setattr(
        db_mod, "get_settings",
        lambda: SimpleNamespace(spare_dsn="postgresql+asyncpg://u:p@spare:5432/db"),
    )
    assert db_mod.active_db() == "main"
    assert await db_mod.activate_spare() is True
    assert db_mod.active_db() == "spare"
    # the shared sessionmaker now points at the spare host
    s = db_mod.AsyncSessionLocal()
    assert s.bind.url.host == "spare"
    await s.close()


@pytest.mark.asyncio
async def test_activate_spare_no_dsn_stays_main(monkeypatch):
    monkeypatch.setattr(db_mod, "get_settings", lambda: SimpleNamespace(spare_dsn=""))
    assert await db_mod.activate_spare() is False
    assert db_mod.active_db() == "main"


# --- supervisor: fail over after threshold ---------------------------------


@pytest.mark.asyncio
async def test_supervisor_fails_over_after_threshold(monkeypatch):
    app = _app()
    monkeypatch.setattr(db_failover, "_probe", AsyncMock(return_value=False))
    do = AsyncMock()
    monkeypatch.setattr(db_failover, "_do_failover", do)
    monkeypatch.setattr(db_failover, "get_settings", _supervisor_settings)

    stop = asyncio.Event()
    task = asyncio.create_task(db_failover.run_failover_supervisor(app, stop))
    await asyncio.sleep(0.1)
    stop.set()
    await task
    assert do.await_count >= 1


@pytest.mark.asyncio
async def test_supervisor_healthy_main_never_fails_over(monkeypatch):
    app = _app()
    monkeypatch.setattr(db_failover, "_probe", AsyncMock(return_value=True))
    do = AsyncMock()
    monkeypatch.setattr(db_failover, "_do_failover", do)
    monkeypatch.setattr(db_failover, "_clear_marker", lambda p: None)
    monkeypatch.setattr(db_failover, "get_settings", _supervisor_settings)

    stop = asyncio.Event()
    task = asyncio.create_task(db_failover.run_failover_supervisor(app, stop))
    await asyncio.sleep(0.08)
    stop.set()
    await task
    assert do.await_count == 0


# --- no auto-failback -------------------------------------------------------


@pytest.mark.asyncio
async def test_no_auto_failback_alerts_once(monkeypatch):
    monkeypatch.setattr(
        db_mod, "get_settings",
        lambda: SimpleNamespace(spare_dsn="postgresql+asyncpg://u:p@spare:5432/db"),
    )
    await db_mod.activate_spare()
    assert db_mod.active_db() == "spare"

    app = _app()
    monkeypatch.setattr(db_failover, "_probe", AsyncMock(return_value=True))  # main is back
    sig = AsyncMock()
    monkeypatch.setattr(db_failover, "_raise_signal", sig)
    monkeypatch.setattr(db_failover, "get_settings", _supervisor_settings)

    stop = asyncio.Event()
    task = asyncio.create_task(db_failover.run_failover_supervisor(app, stop))
    await asyncio.sleep(0.08)
    stop.set()
    await task
    assert db_mod.active_db() == "spare"  # NEVER auto-failed-back
    assert sig.await_count == 1  # alerted exactly once that the main is back


# --- _do_failover: marker + state + re-election ----------------------------


@pytest.mark.asyncio
async def test_do_failover_writes_marker_and_reelects(monkeypatch, tmp_path):
    app = _app()
    marker = tmp_path / "FAILOVER_ACTIVE"
    monkeypatch.setattr(db_mod, "activate_spare", AsyncMock(return_value=True))
    monkeypatch.setattr(
        db_failover, "get_settings",
        lambda: SimpleNamespace(
            resolved_failover_marker_path=lambda: str(marker),
            enable_worker_leader_election=True,
        ),
    )
    acquire = AsyncMock(return_value=True)
    monkeypatch.setattr(db_failover, "acquire_worker_leadership", acquire)
    monkeypatch.setattr(db_failover, "release_worker_leadership", AsyncMock())
    monkeypatch.setattr(db_failover, "_raise_signal", AsyncMock())

    await db_failover._do_failover(app)

    assert marker.exists()  # cron-clobber guard written
    assert app.state.active_db == "spare"
    assert app.state.failed_over_at is not None
    assert acquire.await_count == 1  # leadership re-elected on the spare


@pytest.mark.asyncio
async def test_do_failover_no_spare_is_noop(monkeypatch, tmp_path):
    app = _app()
    marker = tmp_path / "FAILOVER_ACTIVE"
    monkeypatch.setattr(db_mod, "activate_spare", AsyncMock(return_value=False))
    monkeypatch.setattr(
        db_failover, "get_settings",
        lambda: SimpleNamespace(
            resolved_failover_marker_path=lambda: str(marker),
            enable_worker_leader_election=True,
        ),
    )
    await db_failover._do_failover(app)
    assert not marker.exists()  # no spare -> nothing written, still on main
    assert app.state.active_db == "main"


# --- cron-clobber: run_backup skips while failed over ----------------------


def test_run_backup_skips_when_marker_present(tmp_path):
    marker = tmp_path / "FAILOVER_ACTIVE"
    marker.write_text("2026-06-21T00:00:00Z\n")
    calls = []
    out = db_backup.run_backup(
        main_dsn="m", spare_dsn="s", dump_dir=str(tmp_path), keep=4,
        marker_path=str(marker),
        dump_fn=lambda a, b: calls.append("dump"),
        restore_fn=lambda a, b: calls.append("restore"),
    )
    assert out.get("skipped") == "failover_active"
    assert calls == []  # neither dump nor restore touched the live spare


def test_run_backup_runs_when_no_marker(tmp_path):
    calls = []
    out = db_backup.run_backup(
        main_dsn="m@host", spare_dsn="s", dump_dir=str(tmp_path), keep=4,
        marker_path=str(tmp_path / "nope"),
        dump_fn=lambda a, b: (calls.append("dump"), open(b, "wb").close()),
        restore_fn=lambda a, b: calls.append("restore"),
    )
    assert "skipped" not in out
    assert calls == ["dump", "restore"]
