"""Cursor filter logic for list_new_files.

We mock the SSH call so the test stays pure. The actual filter behaviour
to pin:

* mtime > last_mtime - 5s (5-second slop)
* filename > last_filename (precise, second-line defense)
* result is sorted by (mtime, name) ascending

Note: ``run_command`` is lazy-imported inside ``list_new_files``
(see ``trade_ingestor.py`` line ~119), so we patch it at the SOURCE
module ``app.services.ssh.commands`` — patching ``trade_ingestor.svc``
attribute would silently no-op because it doesn't exist there at
module load.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def _fake_server():
    s = MagicMock()
    s.id = "fake-server"
    return s


@pytest.fixture
def _fake_stack():
    s = MagicMock()
    s.id = "fake-stack"
    s.stack_dir = "/root/seller-market/agents/abc"
    return s


def _mk_run_command_output(files: list[tuple[int, str]]):
    """Return a fake run_command coroutine that emits 'mtime path' per file."""
    body = "\n".join(f"{ts} {path}" for ts, path in files)

    async def _fake_run(*args, **kwargs):
        result = MagicMock()
        result.stdout = body
        return result

    return _fake_run


@pytest.mark.asyncio
async def test_no_cursor_returns_all_files(_fake_server, _fake_stack):
    from app.services import trade_ingestor as svc

    base_dir = _fake_stack.stack_dir + "/order_results"
    fake_files = [
        (1_700_000_100, f"{base_dir}/a_bbi_20260101_000001.json"),
        (1_700_000_200, f"{base_dir}/b_bbi_20260101_000002.json"),
    ]
    with patch(
        "app.services.ssh.commands.run_command",
        new=_mk_run_command_output(fake_files),
    ):
        files = await svc.list_new_files(
            _fake_server, _fake_stack, last_filename=None, last_mtime=None,
        )
    assert len(files) == 2
    assert files[0].name == "a_bbi_20260101_000001.json"
    assert files[1].name == "b_bbi_20260101_000002.json"


@pytest.mark.asyncio
async def test_cursor_filters_older_files(_fake_server, _fake_stack):
    from app.services import trade_ingestor as svc

    base_dir = _fake_stack.stack_dir + "/order_results"
    fake_files = [
        (1_700_000_100, f"{base_dir}/a_bbi_20260101_000001.json"),
        (1_700_000_200, f"{base_dir}/b_bbi_20260101_000002.json"),
        (1_700_000_300, f"{base_dir}/c_bbi_20260101_000003.json"),
    ]
    last_mtime = datetime.fromtimestamp(1_700_000_150, tz=timezone.utc)
    with patch(
        "app.services.ssh.commands.run_command",
        new=_mk_run_command_output(fake_files),
    ):
        files = await svc.list_new_files(
            _fake_server, _fake_stack,
            last_filename="a_bbi_20260101_000001.json",
            last_mtime=last_mtime,
        )
    # The 5-second mtime slop means files with mtime > (last_mtime - 5s)
    # are kept; the precise filename filter then rejects "a_*".
    names = [f.name for f in files]
    assert "a_bbi_20260101_000001.json" not in names
    assert "b_bbi_20260101_000002.json" in names
    assert "c_bbi_20260101_000003.json" in names


@pytest.mark.asyncio
async def test_result_sorted_by_mtime_then_name(_fake_server, _fake_stack):
    from app.services import trade_ingestor as svc

    base_dir = _fake_stack.stack_dir + "/order_results"
    # Same mtime, different name; should sort by name.
    fake_files = [
        (1_700_000_200, f"{base_dir}/b_bbi_20260101_000002.json"),
        (1_700_000_100, f"{base_dir}/a_bbi_20260101_000001.json"),
        (1_700_000_200, f"{base_dir}/c_bbi_20260101_000002.json"),
    ]
    with patch(
        "app.services.ssh.commands.run_command",
        new=_mk_run_command_output(fake_files),
    ):
        files = await svc.list_new_files(
            _fake_server, _fake_stack, last_filename=None, last_mtime=None,
        )
    assert [f.name for f in files] == [
        "a_bbi_20260101_000001.json",  # earliest mtime
        "b_bbi_20260101_000002.json",  # same mtime, alphabetically first
        "c_bbi_20260101_000002.json",
    ]


@pytest.mark.asyncio
async def test_unparseable_filenames_skipped(_fake_server, _fake_stack):
    from app.services import trade_ingestor as svc

    base_dir = _fake_stack.stack_dir + "/order_results"
    fake_files = [
        (1_700_000_100, f"{base_dir}/garbage.json"),                  # bad
        (1_700_000_200, f"{base_dir}/b_bbi_20260101_000002.json"),    # good
    ]
    with patch(
        "app.services.ssh.commands.run_command",
        new=_mk_run_command_output(fake_files),
    ):
        files = await svc.list_new_files(
            _fake_server, _fake_stack, last_filename=None, last_mtime=None,
        )
    assert len(files) == 1
    assert files[0].name == "b_bbi_20260101_000002.json"
