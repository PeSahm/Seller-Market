"""Tests for scheduler's full-run-log emission (gzipped, marker-adjacent).

The mgmt UI's scheduled_run_ingestor fetches ``scheduled_run_<uuid>.log.gz``
named by the marker's ``log_file`` field and archives it verbatim, so the
operator can download COMPLETE scheduled-run logs (markers only carry 4 KB
tails as a fallback).
"""
import gzip
import os
import time

import scheduler


def test_write_gz_with_stderr_separator(tmp_path):
    path = str(tmp_path / "scheduled_run_x.log.gz")
    ok = scheduler._write_scheduled_run_log_gz(path, "out line\n", "err line\n")
    assert ok is True
    blob = gzip.decompress(open(path, "rb").read())
    # Byte-identical separator to mgmt's finalize_run / _archive_log_if_final.
    assert blob == b"out line\n" + b"\n--- stderr ---\n" + b"err line\n"
    assert not os.path.exists(path + ".tmp")  # atomic write, no leftovers


def test_write_gz_without_stderr_has_no_separator(tmp_path):
    path = str(tmp_path / "scheduled_run_y.log.gz")
    assert scheduler._write_scheduled_run_log_gz(path, "only stdout\n", "") is True
    assert gzip.decompress(open(path, "rb").read()) == b"only stdout\n"


def test_write_gz_refuses_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_RUN_LOG_MAX_BYTES", 16)
    path = str(tmp_path / "scheduled_run_z.log.gz")
    assert scheduler._write_scheduled_run_log_gz(path, "X" * 1000, "") is False
    assert not os.path.exists(path)


def test_prune_old_run_log_gz(tmp_path):
    fresh = tmp_path / "scheduled_run_fresh.log.gz"
    stale = tmp_path / "scheduled_run_stale.log.gz"
    other = tmp_path / "order_fires_20260610.jsonl"  # must never be touched
    for p in (fresh, stale, other):
        p.write_bytes(b"x")
    old = time.time() - 10 * 86400
    os.utime(stale, (old, old))
    os.utime(other, (old, old))

    scheduler._prune_old_run_log_gz(str(tmp_path), max_age_days=7)

    assert fresh.exists()
    assert not stale.exists()
    assert other.exists()
