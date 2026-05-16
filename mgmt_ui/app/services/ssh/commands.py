"""Run short remote commands over the pooled SSH transport.

The mgmt UI uses this for one-shot commands: ``test -d``, ``date +%s``,
``docker compose up -d``, ``docker exec -d ...``. Long-running streaming
operations (``docker logs -f``) belong to Phase 6's WebSocket log streamer,
which opens its own exec channel on top of :func:`app.services.ssh.pool.acquire`.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass
from typing import Optional

import paramiko

from app.models.servers import Server
from app.services.ssh.exceptions import (
    AuthenticationError,
    RemoteCommandError,
    SSHConnectionError,
)
from app.services.ssh.pool import ssh_pool

logger = logging.getLogger(__name__)


@dataclass
class RemoteResult:
    """Outcome of a single :func:`run_command` call.

    ``stdout`` and ``stderr`` are decoded as UTF-8 with ``errors='replace'``
    so a stray binary byte never crashes the caller.
    """

    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """``True`` iff the remote process exited with status 0."""
        return self.exit_code == 0


async def run_command(
    server: Server,
    command: str,
    *,
    timeout: float = 30.0,
    check: bool = False,
    stdin_data: Optional[bytes] = None,
) -> RemoteResult:
    """Run a single remote command and return its result.

    Args:
        server: Target server record.
        command: Shell command line (executed by the remote login shell).
        timeout: Channel-level timeout in seconds. Applies to each blocking
            paramiko read; total wall-clock may be longer if the remote
            process is producing output steadily.
        check: If ``True`` and the exit code is non-zero, raise
            :class:`RemoteCommandError`.
        stdin_data: Optional bytes to send on stdin before half-closing.

    Returns:
        :class:`RemoteResult` with ``exit_code``, ``stdout``, ``stderr``.

    Raises:
        RemoteCommandError: if ``check=True`` and ``exit_code != 0``.
    """
    async with ssh_pool.session(server) as client:

        def _exec() -> RemoteResult:
            try:
                stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
                try:
                    if stdin_data is not None:
                        stdin.write(stdin_data)
                        stdin.flush()
                    # Always half-close stdin so the remote command sees EOF.
                    try:
                        stdin.channel.shutdown_write()
                    except Exception:  # noqa: BLE001
                        pass
                    # Drain stdout/stderr BEFORE recv_exit_status to avoid a
                    # deadlock when the remote command's output exceeds the
                    # paramiko channel window (~2 MiB). Both reads block until
                    # EOF, which the remote side signals at process exit.
                    out = stdout.read().decode("utf-8", errors="replace")
                    err = stderr.read().decode("utf-8", errors="replace")
                    exit_code = stdout.channel.recv_exit_status()
                finally:
                    try:
                        stdin.close()
                    except Exception:  # noqa: BLE001
                        pass
                return RemoteResult(exit_code=exit_code, stdout=out, stderr=err)
            except paramiko.AuthenticationException as exc:
                raise AuthenticationError(str(exc)) from exc
            except paramiko.SSHException as exc:
                raise SSHConnectionError(str(exc)) from exc
            except (socket.timeout, TimeoutError) as exc:
                raise SSHConnectionError(
                    f"command timed out: {command!r}"
                ) from exc
            except (socket.error, OSError) as exc:
                raise SSHConnectionError(str(exc)) from exc

        result = await asyncio.to_thread(_exec)

    if check and not result.ok:
        # Log at DEBUG only — stderr may contain sensitive material.
        logger.debug(
            "remote command failed: exit=%d cmd=%r", result.exit_code, command
        )
        raise RemoteCommandError(
            command=command,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return result
