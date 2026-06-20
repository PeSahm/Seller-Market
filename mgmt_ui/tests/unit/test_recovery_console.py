"""Tests for the DB-independent recovery console (#156).

These build the app with ``MGMT_RECOVERY_MODE=true`` and drive it with a
TestClient — confirming it boots WITHOUT any database, gates on the token, lists
backups from the manifest, and guards the restore endpoint.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import settings as settings_mod
from app.services import db_backup


@pytest.fixture
def recovery_app(tmp_path, monkeypatch):
    monkeypatch.setenv("MGMT_RECOVERY_MODE", "true")
    monkeypatch.setenv("MGMT_RECOVERY_TOKEN", "s3cret-token")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("SPARE_DSN", "postgresql://mgmt:pw@localhost:5432/mgmt_ui")
    # Required settings fields (unused in recovery mode, but Settings() needs them):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@nohost:5432/db")
    monkeypatch.setenv("MGMT_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("MGMT_FERNET_KEY_PART1", "x" * 43)
    monkeypatch.setenv("MGMT_CSRF_SECRET", "y" * 40)
    settings_mod.get_settings.cache_clear()

    from app.main import create_app

    app = create_app()
    yield app
    settings_mod.get_settings.cache_clear()


def test_boots_in_recovery_mode_without_db(recovery_app):
    assert "RECOVERY" in recovery_app.title
    client = TestClient(recovery_app)
    assert client.get("/health").json() == {"status": "recovery"}
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/recovery"


def test_token_gate(recovery_app):
    client = TestClient(recovery_app)
    # no token -> 401 + form
    r = client.get("/recovery")
    assert r.status_code == 401
    assert "recovery token" in r.text.lower()
    # wrong token -> 401
    assert client.get("/recovery", params={"token": "nope"}).status_code == 401
    # right token -> console
    r = client.get("/recovery", params={"token": "s3cret-token"})
    assert r.status_code == 200
    assert "Backups" in r.text


def test_console_lists_manifest(recovery_app, tmp_path):
    db_backup.append_manifest(
        tmp_path / db_backup.MANIFEST_NAME,
        {"file": "mgmt_20260621T080000Z.dump", "taken_at": "2026-06-21T08:00:00+00:00",
         "size": 5_500_000, "sha256": "abc", "source": "main", "restored_ok": True},
        keep=10,
    )
    client = TestClient(recovery_app)
    r = client.get("/recovery", params={"token": "s3cret-token"})
    assert "mgmt_20260621T080000Z.dump" in r.text
    assert "5.2 MB" in r.text  # 5_500_000 bytes


def test_restore_requires_token(recovery_app):
    client = TestClient(recovery_app)
    r = client.post("/recovery/restore", data={"token": "nope", "file": "x.dump"})
    assert r.status_code == 401


def test_restore_path_traversal_guard(recovery_app):
    client = TestClient(recovery_app)
    r = client.post("/recovery/restore", data={"token": "s3cret-token", "file": "../etc/passwd"})
    assert "Invalid backup file name" in r.text


def test_restore_calls_restore_dump(recovery_app, tmp_path, monkeypatch):
    dump = tmp_path / "mgmt_x.dump"
    dump.write_bytes(b"FAKE")
    called = {}
    monkeypatch.setattr(
        db_backup, "restore_dump",
        lambda path, dsn, **kw: called.update(path=str(path), dsn=dsn),
    )
    client = TestClient(recovery_app)
    r = client.post("/recovery/restore", data={"token": "s3cret-token", "file": "mgmt_x.dump"})
    assert r.status_code == 200
    assert "Restored mgmt_x.dump" in r.text
    assert called["path"].endswith("mgmt_x.dump")
    assert "localhost:5432" in called["dsn"]
