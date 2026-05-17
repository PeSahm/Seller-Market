"""Unit tests for sftp_atomic_write.

Verifies the in-place truncate-write pattern and the path guard.
Paramiko is fully mocked so no real SSH happens.

The helper used to do tmp+rename for crisper atomicity, but docker
single-file bind mounts staple to the destination inode at container
start time and never re-resolve after a rename — so renames invisibly
break the container's view. We switched to in-place writes; readers
that catch JSON / configparser errors tolerate the small partial-read
window during the write.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.ssh.exceptions import PathOutOfScopeError


class _FakeServer:
    id = "fake-server"
    base_dir = "/root/seller-market/agents"


@pytest.mark.asyncio
async def test_writes_in_place_to_destination() -> None:
    """Verify the helper writes directly to the destination (no tmp+rename).

    docker single-file bind mounts staple to the destination inode at
    container start. A rename swaps the inode and orphans the bind mount,
    so the container would see frozen content forever. Writing in place
    truncates and re-fills the existing inode, which the container sees
    immediately.
    """
    from app.services.ssh import sftp as sftp_module

    fake_client = MagicMock()

    fake_sftp_file = MagicMock()
    fake_sftp_file.__enter__.return_value = fake_sftp_file
    fake_sftp_file.__exit__.return_value = False

    fake_sftp_obj = MagicMock()
    fake_sftp_obj.file.return_value = fake_sftp_file
    fake_client.open_sftp.return_value = fake_sftp_obj

    fake_stdout = MagicMock()
    fake_stdout.channel.recv_exit_status.return_value = 0
    fake_stderr = MagicMock()
    fake_stderr.read.return_value = b""
    fake_stdin = MagicMock()
    fake_client.exec_command.return_value = (fake_stdin, fake_stdout, fake_stderr)

    class _Session:
        async def __aenter__(self) -> MagicMock:
            return fake_client

        async def __aexit__(self, *a: object) -> bool:
            return False

    fake_pool = MagicMock()
    fake_pool.session = lambda server: _Session()

    target = "/root/seller-market/agents/1/config.ini"

    with patch.object(sftp_module, "ssh_pool", fake_pool):
        await sftp_module.sftp_atomic_write(
            _FakeServer(),
            target,
            "hello world",
        )

    # Opened the destination path itself with 'wb' (O_WRONLY|O_TRUNC|O_CREAT)
    # — same inode, just truncated. NO ".tmp" suffix anywhere.
    fake_sftp_obj.file.assert_called_with(target, "wb")
    fake_sftp_file.write.assert_called_once_with(b"hello world")

    # chmod applied to the destination (re-used) inode.
    fake_sftp_obj.chmod.assert_called_once()
    chmod_args = fake_sftp_obj.chmod.call_args[0]
    assert chmod_args[0] == target
    # Just `sync` — no rename.
    cmd = fake_client.exec_command.call_args[0][0]
    assert cmd == "sync"
    # And there is NO .tmp file referenced anywhere in any call.
    assert all(
        ".tmp" not in str(c) for c in fake_sftp_obj.file.call_args_list
    ), "no .tmp file should be created — that breaks docker single-file bind mounts"


@pytest.mark.asyncio
async def test_rejects_out_of_scope_path() -> None:
    """sftp_atomic_write must refuse writes outside server.base_dir."""
    from app.services.ssh.sftp import sftp_atomic_write

    with pytest.raises(PathOutOfScopeError):
        await sftp_atomic_write(_FakeServer(), "/etc/passwd", "evil")


def test_shell_quote_handles_single_quotes() -> None:
    """The mv command must safely quote paths even with single quotes."""
    from app.services.ssh.sftp import _shell_quote

    assert _shell_quote("a'b") == "'a'\\''b'"


def test_shell_quote_simple_path() -> None:
    from app.services.ssh.sftp import _shell_quote

    assert _shell_quote("/root/seller-market/agents/1/config.ini") == (
        "'/root/seller-market/agents/1/config.ini'"
    )
