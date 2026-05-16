"""Public API of the SSH service layer.

Routers and other higher-level code should import from this module rather
than from the submodules directly, so we have a single chokepoint when we
need to swap implementations (e.g. mocked clients in tests).
"""

from app.services.ssh.commands import RemoteResult, run_command
from app.services.ssh.exceptions import (
    AuthenticationError,
    HostKeyMismatchError,
    PathOutOfScopeError,
    RemoteCommandError,
    SSHConnectionError,
    SSHError,
)
from app.services.ssh.pool import SSHPool, fingerprint, ssh_pool
from app.services.ssh.sftp import sftp_atomic_write, sftp_read_text

__all__ = [
    # Exceptions
    "SSHError",
    "SSHConnectionError",
    "HostKeyMismatchError",
    "AuthenticationError",
    "PathOutOfScopeError",
    "RemoteCommandError",
    # Pool
    "SSHPool",
    "ssh_pool",
    "fingerprint",
    # SFTP
    "sftp_atomic_write",
    "sftp_read_text",
    # Commands
    "run_command",
    "RemoteResult",
]
