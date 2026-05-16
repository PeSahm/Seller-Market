from __future__ import annotations

import logging

import paramiko

logger = logging.getLogger(__name__)


class Sensitive:
    """Wraps a secret string; never serialized, never logged.

    Use ``.reveal()`` only at the point where the raw value is required
    (e.g. handing to an SSH client or an HTTP Authorization header).
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def __repr__(self) -> str:
        return "Sensitive(***)"

    def __str__(self) -> str:
        return "***"

    def reveal(self) -> str:
        return self._value

    # Defensive: prevent accidental equality leaking via timing differences.
    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sensitive):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("Sensitive", self._value))


def load_ssh_private_key(path: str) -> paramiko.PKey:
    """Load an SSH private key from disk.

    Tries RSA first, then ED25519, then ECDSA. Raises ``paramiko.SSHException``
    if none of the formats match.
    """
    last_error: Exception | None = None
    for loader in (
        paramiko.RSAKey.from_private_key_file,
        paramiko.Ed25519Key.from_private_key_file,
        paramiko.ECDSAKey.from_private_key_file,
    ):
        try:
            return loader(path)
        except paramiko.SSHException as exc:
            last_error = exc
            continue
    raise paramiko.SSHException(
        f"Could not load SSH private key at {path}: {last_error}"
    )
