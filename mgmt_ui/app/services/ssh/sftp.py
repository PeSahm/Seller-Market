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
import socket
from typing import Union

import paramiko

from app.models.servers import Server
from app.services.ssh.exceptions import (
    AuthenticationError,
    PathOutOfScopeError,
    SSHConnectionError,
    SSHError,
)
from app.services.ssh.pool import ssh_pool

logger = logging.getLogger(__name__)

_BytesOrStr = Union[bytes, str]


# ---------------------------------------------------------------------------
# Path scoping
# ---------------------------------------------------------------------------


def _assert_path_in_scope(server: Server, remote_path: str) -> None:
    """Refuse any write whose path isn't strictly under the server's allowed prefix.

    ``base_dir`` is normalised via :func:`posixpath.normpath` (remote is Linux).
    Two extra safeguards over a naive prefix check:

    * **Root ``base_dir`` is rejected outright.** ``normpath("/").rstrip("/")``
      collapses to ``""``, and ``"" + "/"`` is a prefix of every absolute path
      — meaning a misconfigured ``base_dir="/"`` would silently allow writes
      anywhere on the box. We refuse to operate against such a server.
    * **``remote_path == base_dir`` is rejected.** The atomic-write helper
      stages content at ``<remote_path>.tmp`` before renaming; if
      ``remote_path`` *were* ``base_dir``, the staged tmp would live at
      ``<base_dir>.tmp`` — a sibling of, not under, the scope. Callers must
      write actual files under ``base_dir``, never to ``base_dir`` itself.
    """
    base = posixpath.normpath(server.base_dir)
    if base in ("/", "", "."):
        raise PathOutOfScopeError(
            f"server.base_dir must be a non-root absolute path; got "
            f"{server.base_dir!r}"
        )
    norm = posixpath.normpath(remote_path)
    if not norm.startswith(base + "/"):
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
    """Write ``content`` to ``remote_path`` in-place (truncate + rewrite).

    **Why not tmp + rename?** The earlier implementation wrote to
    ``<remote_path>.tmp`` and then ``mv -f``'d it into place, giving us a
    crisp atomic-rename guarantee. That guarantee is great for general
    POSIX usage but is **broken by docker single-file bind mounts**: a
    rename swaps the destination inode, while the container's bind mount
    is stapled to the original inode at ``docker run`` time and never
    re-resolves. Result: the host file updates, but every container
    forever reads the original frozen content.

    The bot's ``load_config`` already wraps the JSON parse in try/except
    and falls back to ``{"enabled": false, "jobs": []}`` for one tick on
    a partial read — so a torn read window of a few hundred microseconds
    is harmless. The next 1-second poll picks up the complete file.

    Strategy:

    1. ``sftp.file(remote_path, "wb")`` opens the EXISTING inode with
       ``O_WRONLY | O_TRUNC | O_CREAT`` — same inode as before, just
       truncated.
    2. Write the new payload to that inode.
    3. ``chmod`` the (re-used) inode.
    4. SFTP-close, then ``sync`` over a separate exec to flush to disk.

    Trade-offs vs. the old tmp+rename:

    * **Visible to docker bind-mounted containers** ✓ (the main reason)
    * **Atomicity** ✗ — readers can see a partial / empty file during the
      ~ms write window. Bot tolerates this via try/except + 1-second
      retry. Don't use this helper for files where partial reads would
      be catastrophic.
    * **Crash resilience** ≈ similar — both strategies can lose a write
      to a power cut; the ``sync`` at the end is the same.

    Raises:
        PathOutOfScopeError: if ``remote_path`` is outside ``server.base_dir``.
        SSHError: if the SFTP write or chmod fails.
    """
    _assert_path_in_scope(server, remote_path)

    payload: bytes
    if isinstance(content, str):
        payload = content.encode("utf-8")
    else:
        payload = content

    def _write(client: paramiko.SSHClient) -> None:
        # paramiko ChannelException / SSHException are NOT caught here so
        # run_with_retry can evict a stale transport and retry once.
        try:
            sftp: paramiko.SFTPClient = client.open_sftp()
            try:
                # 'wb' = O_WRONLY|O_CREAT|O_TRUNC — re-uses the
                # existing inode if the file is already there, which
                # is the whole point of this rewrite. Paramiko's
                # set_pipelined(False) trades a tiny bit of throughput
                # for a stricter "data hit the wire" guarantee.
                with sftp.file(remote_path, "wb") as fh:
                    fh.set_pipelined(False)
                    fh.write(payload)
                sftp.chmod(remote_path, mode)
            finally:
                try:
                    sftp.close()
                except Exception:  # noqa: BLE001
                    pass

            # Best-effort durability flush so a power cut right after
            # the SFTP close doesn't lose the write.
            stdin, stdout, stderr = client.exec_command("sync", timeout=30)
            try:
                stdout.channel.recv_exit_status()
            finally:
                try:
                    stdin.close()
                except Exception:  # noqa: BLE001
                    pass
        except (socket.timeout, TimeoutError) as exc:
            raise SSHConnectionError("sftp timed out") from exc
        except (socket.error, OSError, IOError) as exc:
            raise SSHError(f"sftp i/o failed: {exc}") from exc

    try:
        await ssh_pool.run_with_retry(server, _write)
    except paramiko.AuthenticationException as exc:
        raise AuthenticationError(str(exc)) from exc
    except paramiko.SSHException as exc:
        raise SSHConnectionError(str(exc) or exc.__class__.__name__) from exc


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
    def _read(client: paramiko.SSHClient) -> str:
        # ChannelException / SSHException propagate raw so run_with_retry
        # can evict a stale transport and retry on a fresh one.
        try:
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
        except (socket.timeout, TimeoutError) as exc:
            raise SSHConnectionError("sftp timed out") from exc
        except (socket.error, OSError, IOError) as exc:
            raise SSHError(f"sftp i/o failed: {exc}") from exc

    try:
        return await ssh_pool.run_with_retry(server, _read)
    except paramiko.AuthenticationException as exc:
        raise AuthenticationError(str(exc)) from exc
    except paramiko.SSHException as exc:
        raise SSHConnectionError(str(exc) or exc.__class__.__name__) from exc
