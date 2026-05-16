"""Atomic SFTP write + read helpers.

The mgmt UI uses these to ship small config files (``config.ini``,
``scheduler_config.json``, ``locust_config.json``) to remote trading servers.
Writes are atomic from the consumer's point of view: a watcher tailing the
final path either sees the old contents or the new contents, never a partial
write.

Path-scope guard
----------------
Every write is checked against ``server.base_dir``. The mgmt UI MUST NOT
clobber the existing ``/root/seller-market/`` root-level deployment — only
agent-scoped files under ``base_dir`` (typically ``/root/seller-market/agents``)
are writeable. Reads are not guarded because admins may legitimately need to
inspect arbitrary remote files.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from typing import Union

import paramiko

from app.models.servers import Server
from app.services.ssh.exceptions import PathOutOfScopeError, SSHError
from app.services.ssh.pool import ssh_pool

logger = logging.getLogger(__name__)

_BytesOrStr = Union[bytes, str]


# ---------------------------------------------------------------------------
# Path scoping
# ---------------------------------------------------------------------------


def _assert_path_in_scope(server: Server, remote_path: str) -> None:
    """Refuse any write whose path doesn't start with the server's allowed prefix.

    ``base_dir`` is normalised via :func:`posixpath.normpath` (remote is Linux)
    and trailing slashes are stripped. ``remote_path`` is likewise normalised,
    then required to either equal ``base_dir`` or begin with ``base_dir + "/"``.
    A path of exactly ``base_dir`` is allowed because callers may legitimately
    write a marker file into the directory itself.
    """
    base = posixpath.normpath(server.base_dir).rstrip("/")
    norm = posixpath.normpath(remote_path)
    if norm == base or norm.startswith(base + "/"):
        return
    raise PathOutOfScopeError(
        f"refusing to write outside server scope: {remote_path!r} "
        f"(base_dir={base!r})"
    )


# ---------------------------------------------------------------------------
# Shell quoting (for the rename step)
# ---------------------------------------------------------------------------


def _shell_quote(s: str) -> str:
    r"""Single-quote a string for safe POSIX shell use.

    Mirrors :func:`shlex.quote` for the single-quote case: wrap in ``'...'``,
    escape any internal ``'`` as ``'\''``. Example: ``a'b`` -> ``'a'\''b'``.
    """
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def sftp_atomic_write(
    server: Server,
    remote_path: str,
    content: _BytesOrStr,
    *,
    mode: int = 0o644,
) -> None:
    """Write ``content`` to ``remote_path`` atomically.

    Strategy:

    1. Upload bytes to ``<remote_path>.tmp`` via SFTP, ``chmod`` to ``mode``.
    2. ``mv -f <tmp> <final> && sync`` over a single exec channel — a same-FS
       rename is a single ``renameat2`` syscall, hence atomic on Linux.
    3. ``sync`` ensures the rename is durable before we return.

    SFTP also exposes a ``posix_rename`` extension, but invoking ``mv -f``
    gives us identical semantics plus the ``sync`` at no extra cost.

    Raises:
        PathOutOfScopeError: if ``remote_path`` is outside ``server.base_dir``.
        SSHError: if the temp upload or rename fails.
    """
    _assert_path_in_scope(server, remote_path)

    payload: bytes
    if isinstance(content, str):
        payload = content.encode("utf-8")
    else:
        payload = content

    tmp_path = remote_path + ".tmp"

    async with ssh_pool.session(server) as client:

        def _write() -> None:
            sftp: paramiko.SFTPClient = client.open_sftp()
            try:
                with sftp.file(tmp_path, "wb") as fh:
                    # Force a flush+fsync at SFTP layer too — paramiko's
                    # SFTPFile honours .set_pipelined() for speed but here we
                    # want correctness over throughput.
                    fh.set_pipelined(False)
                    fh.write(payload)
                sftp.chmod(tmp_path, mode)
            finally:
                try:
                    sftp.close()
                except Exception:  # noqa: BLE001
                    pass

            # Atomic same-FS rename + durability flush.
            cmd = (
                f"mv -f {_shell_quote(tmp_path)} {_shell_quote(remote_path)} "
                f"&& sync"
            )
            stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
            try:
                exit_code = stdout.channel.recv_exit_status()
                if exit_code != 0:
                    err = stderr.read().decode("utf-8", errors="replace")
                    # Best-effort cleanup of the orphan tmp file.
                    _best_effort_unlink(client, tmp_path)
                    raise SSHError(
                        f"atomic rename failed (exit {exit_code}): {err.strip()}"
                    )
            finally:
                try:
                    stdin.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.to_thread(_write)


def _best_effort_unlink(client: paramiko.SSHClient, path: str) -> None:
    """Try to remove ``path``; swallow any error (we're already in a failure path)."""
    try:
        sftp = client.open_sftp()
        try:
            sftp.remove(path)
        finally:
            sftp.close()
    except Exception:  # noqa: BLE001
        logger.debug("best-effort unlink failed for %s", path, exc_info=True)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def sftp_read_text(server: Server, remote_path: str) -> str:
    """Read a small UTF-8 text file from the remote server.

    No scope check: admins may need to inspect files outside ``base_dir``
    (e.g. the legacy ``/root/seller-market/config.ini``).
    """
    async with ssh_pool.session(server) as client:

        def _read() -> str:
            sftp: paramiko.SFTPClient = client.open_sftp()
            try:
                with sftp.file(remote_path, "rb") as fh:
                    data = fh.read()
            finally:
                try:
                    sftp.close()
                except Exception:  # noqa: BLE001
                    pass
            return data.decode("utf-8")

        return await asyncio.to_thread(_read)
