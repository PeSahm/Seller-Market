"""Tests for log_rotation.rotate_and_truncate.

The 2026-06-10 incident: trading_bot.log was blindly truncated at import, so
a manual re-run destroyed the morning run's evidence. Rotation must archive
the previous content (gzipped, complete) and be safe against the locust
master/worker double-import.
"""
import gzip
import os
import time

from log_rotation import rotate_and_truncate


def _write_aged(path, content: bytes, age_seconds: float = 3600):
    path.write_bytes(content)
    old = time.time() - age_seconds
    os.utime(path, (old, old))


def test_rotates_stale_nonempty_file(tmp_path):
    log = tmp_path / "trading_bot.log"
    archive_dir = tmp_path / "logs"
    _write_aged(log, b"yesterday's run output\n" * 50)

    dest = rotate_and_truncate(str(log), str(archive_dir))

    assert dest is not None and dest.endswith(".log.gz")
    # Archive holds the COMPLETE previous content.
    assert gzip.decompress((tmp_path / "logs" / os.path.basename(dest)).read_bytes()) \
        == b"yesterday's run output\n" * 50
    # Original truncated IN PLACE (same path still exists — bind-mount safe).
    assert log.exists() and log.stat().st_size == 0


def test_missing_and_empty_files_are_noops(tmp_path):
    log = tmp_path / "trading_bot.log"
    assert rotate_and_truncate(str(log), str(tmp_path / "logs")) is None

    log.write_bytes(b"")
    old = time.time() - 3600
    os.utime(log, (old, old))
    assert rotate_and_truncate(str(log), str(tmp_path / "logs")) is None
    assert not (tmp_path / "logs").exists()


def test_fresh_file_is_not_rotated(tmp_path):
    """Double-import guard: locust --processes forks a worker that re-imports
    the locustfile seconds after the master truncated+started writing. The
    worker's rotation attempt must NOT archive the live log."""
    log = tmp_path / "trading_bot.log"
    log.write_bytes(b"the CURRENT run's first lines\n")  # mtime = now

    assert rotate_and_truncate(str(log), str(tmp_path / "logs")) is None
    assert log.read_bytes() == b"the CURRENT run's first lines\n"  # untouched


def test_prune_keeps_only_n_archives(tmp_path):
    log = tmp_path / "cache_warmup.log"
    archive_dir = tmp_path / "logs"

    for i in range(4):
        _write_aged(log, f"run {i}\n".encode())
        assert rotate_and_truncate(str(log), str(archive_dir), keep=2) is not None

    archives = sorted(archive_dir.glob("cache_warmup_*.log.gz"))
    assert len(archives) == 2
    # The two NEWEST survive (lexical timestamp sort == chronological).
    contents = {gzip.decompress(p.read_bytes()) for p in archives}
    assert contents == {b"run 2\n", b"run 3\n"}


def test_archive_dir_collision_degrades_quietly(tmp_path):
    """archive_dir path occupied by a FILE → no exception, no truncation."""
    log = tmp_path / "trading_bot.log"
    _write_aged(log, b"precious evidence\n")
    blocker = tmp_path / "logs"
    blocker.write_bytes(b"i am a file, not a dir")

    assert rotate_and_truncate(str(log), str(blocker)) is None
    # CRITICAL: when archiving is impossible the original must NOT be lost.
    assert log.read_bytes() == b"precious evidence\n"
