"""Unit tests for the DB backup/restore core (``app.services.db_backup``).

The ``pg_dump``/``pg_restore`` subprocess calls are injected so the
orchestration (file naming, manifest, prune, restored_ok) is exercised without
a real database. See issue #156.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.services import db_backup


def test_sha256_file(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello world")
    import hashlib

    assert db_backup.sha256_file(p) == hashlib.sha256(b"hello world").hexdigest()


def test_load_manifest_missing_or_garbled(tmp_path):
    assert db_backup.load_manifest(tmp_path / "nope.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert db_backup.load_manifest(bad) == []
    obj = tmp_path / "obj.json"
    obj.write_text('{"a": 1}')  # a dict, not a list
    assert db_backup.load_manifest(obj) == []


def test_append_manifest_trims_to_keep(tmp_path):
    mpath = tmp_path / db_backup.MANIFEST_NAME
    for i in range(5):
        db_backup.append_manifest(mpath, {"file": f"d{i}.dump"}, keep=3)
    entries = db_backup.load_manifest(mpath)
    assert [e["file"] for e in entries] == ["d2.dump", "d3.dump", "d4.dump"]


def test_prune_dumps_keeps_newest(tmp_path):
    import os
    import time

    for i in range(4):
        f = tmp_path / f"mgmt_{i}.dump"
        f.write_bytes(b"x")
        # stagger mtimes so newest is deterministic
        os.utime(f, (time.time() + i, time.time() + i))
    (tmp_path / "manifest.json").write_text("[]")  # non-.dump, must survive
    deleted = db_backup.prune_dumps(tmp_path, keep=2)
    remaining = sorted(p.name for p in tmp_path.glob("*.dump"))
    assert remaining == ["mgmt_2.dump", "mgmt_3.dump"]
    assert len(deleted) == 2
    assert (tmp_path / "manifest.json").exists()


def _fake_dump(_main_dsn, out_path):
    with open(out_path, "wb") as fh:
        fh.write(b"FAKE_PGDUMP_CONTENT")


def test_run_backup_happy_path(tmp_path):
    restored = []
    entry = db_backup.run_backup(
        main_dsn="postgresql://mgmt:pw@windows-main:65444/mgmt_ui",
        spare_dsn="postgresql://mgmt:pw@localhost:5432/mgmt_ui",
        dump_dir=tmp_path,
        keep=10,
        now=datetime(2026, 6, 21, 8, 30, tzinfo=timezone.utc),
        dump_fn=_fake_dump,
        restore_fn=lambda dump, dsn: restored.append((dump, dsn)),
    )
    assert entry["file"] == "mgmt_20260621T083000Z.dump"
    assert entry["restored_ok"] is True
    assert entry["size"] == len(b"FAKE_PGDUMP_CONTENT")
    assert entry["source"] == "windows-main:65444/mgmt_ui"
    assert len(restored) == 1
    # the manifest now holds the entry
    entries = db_backup.load_manifest(tmp_path / db_backup.MANIFEST_NAME)
    assert entries[-1]["file"] == entry["file"]
    # the dump file is on disk
    assert (tmp_path / entry["file"]).exists()


def test_run_backup_restore_failure_keeps_dump(tmp_path):
    def boom(_dump, _dsn):
        raise RuntimeError("spare unreachable")

    entry = db_backup.run_backup(
        main_dsn="x@main",
        spare_dsn="x@spare",
        dump_dir=tmp_path,
        keep=10,
        dump_fn=_fake_dump,
        restore_fn=boom,
    )
    # restored_ok=False but the dump (backup) is still written + recorded
    assert entry["restored_ok"] is False
    assert (tmp_path / entry["file"]).exists()
    assert db_backup.load_manifest(tmp_path / db_backup.MANIFEST_NAME)[-1]["restored_ok"] is False


def test_restore_dump_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        db_backup.restore_dump(tmp_path / "nope.dump", "x@spare")


def test_restore_dump_calls_restore_fn(tmp_path):
    dump = tmp_path / "mgmt_x.dump"
    dump.write_bytes(b"d")
    seen = []
    db_backup.restore_dump(dump, "pg://spare", restore_fn=lambda d, s: seen.append((d, s)))
    assert seen == [(str(dump), "pg://spare")]
