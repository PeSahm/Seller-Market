"""Pydantic schemas for server CRUD (Phase 2).

These models gate the data shape on the HTTP boundary; the business-logic
layer (:mod:`app.services.servers`) consumes them and the router never sees a
raw form dict.

Secret hygiene
--------------
``ServerCreatePassword.password`` and ``ServerCreatePubkey.private_key`` are
write-only — they are accepted on the create form but MUST never appear on an
outbound :class:`ServerOut`. Likewise, ``ssh_secret_ref`` (the encrypted
ciphertext or on-disk path that the model stores) is deliberately excluded
from :class:`ServerOut` so we can't accidentally leak it via a JSON response
or audit-log payload.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_base_dir(value: str) -> str:
    """Reject anything other than an absolute POSIX path with no ``..`` segments.

    Rules:
    - must start with ``/`` (absolute POSIX)
    - no trailing slash (so the path concatenation logic in the SFTP layer
      doesn't have to special-case it)
    - no ``..`` segments (so we can't be tricked into climbing out of the
      intended root via a crafted prefix)
    - reject embedded NUL bytes — these break shell quoting and have no
      legitimate use in a path
    """
    if not value:
        raise ValueError("base_dir must not be empty")
    if "\x00" in value:
        raise ValueError("base_dir must not contain NUL bytes")
    if not value.startswith("/"):
        raise ValueError("base_dir must be an absolute POSIX path (start with '/')")
    if len(value) > 1 and value.endswith("/"):
        raise ValueError("base_dir must not have a trailing slash")
    # Split on '/' and look for '..' anywhere — also catches things like
    # '/foo/../bar' which posixpath.normpath would silently collapse.
    parts = value.split("/")
    if any(part == ".." for part in parts):
        raise ValueError("base_dir must not contain '..' segments")
    return value


ImagePullPolicy = Literal["always", "missing", "never"]


class ServerCreateBase(BaseModel):
    """Fields common to both auth flavours of server creation."""

    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(min_length=1, max_length=64)
    base_dir: str = Field(
        default="/root/seller-market/agents",
        min_length=1,
        max_length=512,
    )
    # Issue #71 incremental. ``always`` matches the historical behaviour
    # (every redeploy fetches the latest bot image from the registry).
    # Operators with restricted egress to ghcr.io flip this to ``never``
    # for their Iranian-VPS rows and pre-pull via a mirror manually.
    image_pull_policy: ImagePullPolicy = "always"

    @field_validator("base_dir")
    @classmethod
    def _check_base_dir(cls, value: str) -> str:
        return _validate_base_dir(value)


class ServerCreatePassword(ServerCreateBase):
    """Create a server that authenticates via SSH password.

    The password is encrypted with Fernet (split-key) by the service layer
    before the row is committed.
    """

    ssh_auth: Literal["password"] = "password"
    password: str = Field(min_length=1)


class ServerCreatePubkey(ServerCreateBase):
    """Create a server that authenticates via SSH private key.

    The raw private key (PEM / OpenSSH format) is written to a chmod-0600
    file on the API container's disk and the file path is stored in
    ``servers.ssh_secret_ref``.
    """

    ssh_auth: Literal["pubkey"] = "pubkey"
    private_key: str = Field(min_length=10)


class ServerUpdate(BaseModel):
    """Partial update of fields the admin can edit post-create.

    Secrets are re-uploaded via a separate endpoint (Phase 9), not via this
    model — that keeps a generic PATCH from accidentally clobbering keys.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    base_dir: Optional[str] = Field(default=None, min_length=1, max_length=512)
    image_pull_policy: Optional[ImagePullPolicy] = None

    @field_validator("base_dir")
    @classmethod
    def _check_base_dir(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return _validate_base_dir(value)


class ServerOut(BaseModel):
    """Outbound representation of a Server row.

    Deliberately omits ``ssh_secret_ref`` so we cannot accidentally leak the
    encrypted password or on-disk key path via JSON responses or audit
    payloads.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    host: str
    ssh_port: int
    ssh_user: str
    ssh_auth: str
    host_key_pin: Optional[str]
    status: str
    last_seen_at: Optional[datetime]
    base_dir: str
    image_pull_policy: str
    created_at: datetime


class TestConnectionResult(BaseModel):
    """Result payload from the "Test Connection" admin action.

    ``new_pin`` is True iff this call performed a trust-on-first-use pin
    update (i.e. ``host_key_pin`` was previously NULL and is now set). On a
    mismatch we set ``host_key_mismatch=True`` and DO NOT touch the existing
    pin — the admin has to confirm via a separate "rotate pin" flow that
    Phase 9 will add.
    """

    ok: bool
    fingerprint: Optional[str] = None  # SHA256 hex
    new_pin: bool = False
    host_key_mismatch: bool = False
    clock_skew_seconds: Optional[int] = None  # remote - local (positive = remote ahead)
    seller_market_present: Optional[bool] = None  # /root/seller-market/ exists
    docker_version: Optional[str] = None
    # Issue #67: pre-flight check that the SSH user can actually write under
    # the server's ``base_dir``. ``None`` means the probe didn't run (e.g.
    # the SSH layer failed before we got here); ``False`` means write would
    # be denied — provisioning will fail with ``mkdir: Permission denied``
    # and the operator should either switch to a root SSH user, change
    # ``base_dir`` to a path the user owns, or pre-create the directory
    # off-band. ``base_dir_probed`` echoes the directory we tested so the
    # template can name it in the error message. ``ssh_user`` is echoed
    # so the template can render a copy-paste-ready ``chmod``/``chown``
    # fix line (the operator just pastes it on the trading host).
    base_dir_writable: Optional[bool] = None
    base_dir_probed: Optional[str] = None
    ssh_user: Optional[str] = None
    # Pre-rendered copy/paste fix command, built server-side using
    # ``shlex.quote`` for every interpolated token so values containing
    # spaces or shell metacharacters can't produce a broken or dangerous
    # pasted command. ``None`` when ``base_dir_writable`` is not False
    # (success / probe didn't run).
    base_dir_fix_command: Optional[str] = None
    message: str = ""
