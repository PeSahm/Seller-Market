"""Unit tests for sftp_atomic_write.

Verifies the tmp+rename pattern and the path guard. Paramiko is fully mocked
so no real SSH happens.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.ssh.exceptions import PathOutOfScopeError


class _FakeServer:
    id = "fake-server"
    base_dir = "/root/seller-market/agents"


@pytest.mark.asyncio
async def test_writes_via_tmp_then_rename() -> None:
    """Verify the helper writes to <path>.tmp first, then mv -f."""
    from app.services.ssh import sftp as sftp_module

    fake_client = MagicMock()

    # SFTPFile is used as a context manager: `with sftp.file(...) as fh: fh.write(...)`.
    fake_sftp_file = MagicMock()
    fake_sftp_file.__enter__.return_value = fake_sftp_file
    fake_sftp_file.__exit__.return_value = False

    fake_sftp_obj = MagicMock()
    fake_sftp_obj.file.return_value = fake_sftp_file
    fake_client.open_sftp.return_value = fake_sftp_obj

    # exec_command -> (stdin, stdout, stderr); we only need stdout.channel and stderr.read.
    fake_stdout = MagicMock()
    fake_stdout.channel.recv_exit_status.return_value = 0
    fake_stderr = MagicMock()
    fake_stderr.read.return_value = b""
    fake_stdin = MagicMock()
    fake_client.exec_command.return_value = (fake_stdin, fake_stdout, fake_stderr)

    # Stand in for ssh_pool.session(server) -> async context manager yielding client.
    class _Session:
        async def __aenter__(self) -> MagicMock:
            return fake_client

        async def __aexit__(self, *a: object) -> bool:
            return False

    fake_pool = MagicMock()
    fake_pool.session = lambda server: _Session()

    with patch.object(sftp_module, "ssh_pool", fake_pool):
        await sftp_module.sftp_atomic_write(
            _FakeServer(),
            "/root/seller-market/agents/1/config.ini",
            "hello world",
        )

    # Tmp path is opened for binary write.
    fake_sftp_obj.file.assert_called_with(
        "/root/seller-market/agents/1/config.ini.tmp", "wb"
    )
    fake_sftp_file.write.assert_called_once_with(b"hello world")
    # chmod was applied to the tmp path before rename.
    fake_sftp_obj.chmod.assert_called_once()
    chmod_args = fake_sftp_obj.chmod.call_args[0]
    assert chmod_args[0] == "/root/seller-market/agents/1/config.ini.tmp"

    # mv -f and sync are issued via a single exec_command.
    cmd = fake_client.exec_command.call_args[0][0]
    assert ".tmp" in cmd
    assert "mv -f" in cmd
    assert "&& sync" in cmd


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
