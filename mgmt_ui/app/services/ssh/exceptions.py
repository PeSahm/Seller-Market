"""Typed exceptions for the SSH service layer.

These are raised by :mod:`app.services.ssh.pool`, :mod:`app.services.ssh.sftp`,
and :mod:`app.services.ssh.commands`. Routers should translate them into HTTP
responses; never let a raw paramiko exception bubble up past this layer.

Logging note: ``HostKeyMismatchError`` carries fingerprints — callers should
log them only at DEBUG level, never INFO, to avoid leaking host identity into
shared log aggregators.
"""

from __future__ import annotations


class SSHError(Exception):
    """Base for all SSH-layer errors."""


class SSHConnectionError(SSHError):
    """TCP connect / timeout / no route to host."""


class HostKeyMismatchError(SSHError):
    """Remote host key didn't match the pinned value in ``servers.host_key_pin``.

    Carries both the expected and actual SHA256 fingerprints for diagnostics —
    but DO NOT log either at INFO level; this can leak host identity.
    """

    def __init__(self, expected: str, actual: str, host: str) -> None:
        super().__init__(f"host key mismatch for {host}")
        self.expected = expected
        self.actual = actual
        self.host = host


class AuthenticationError(SSHError):
    """Bad password / key rejected, or unrecognised auth method."""


class PathOutOfScopeError(SSHError):
    """SFTP write requested for a path outside the server's allowed prefix.

    The mgmt UI MUST NOT touch the existing ``/root/seller-market/`` root-level
    deployment. Allowed writes must live under ``server.base_dir/...``
    (typically ``/root/seller-market/agents/<id>/``).
    """


class RemoteCommandError(SSHError):
    """Non-zero exit code from a remote command (when ``check=True``)."""

    def __init__(
        self,
        command: str,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(f"command failed (exit {exit_code}): {command}")
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
