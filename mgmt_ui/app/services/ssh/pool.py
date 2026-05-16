"""Paramiko transport pool with host-key pinning and async-friendly wrappers.

Design overview
---------------
Paramiko is a synchronous library. To keep the FastAPI event loop responsive,
every blocking call (``connect``, ``exec_command``, SFTP I/O) is dispatched
through :func:`asyncio.to_thread`. Around that, we layer:

* **One transport per ``(host, port, user)``** — keyed by ``server.id`` and
  cached in a dict guarded by ``asyncio.Lock`` so concurrent requests for the
  same server serialize through a single TCP connection.
* **Trust-on-first-use (TOFU) host-key pinning** — a custom
  :class:`paramiko.MissingHostKeyPolicy` accepts the first key when
  ``server.host_key_pin`` is ``None`` and records the observed SHA256 so the
  caller can persist it; on subsequent connects it must match exactly.
* **Health checks** — a stale/closed transport is detected on each
  :meth:`SSHPool.acquire` and transparently reconnected.

Phase 6 (WebSocket log streaming) will reuse :meth:`SSHPool.acquire` and open
a raw exec channel — there's no need for a separate "streaming pool".
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import paramiko

from app.models.servers import Server
from app.security.crypto import decrypt as fernet_decrypt
from app.services.ssh.exceptions import (
    AuthenticationError,
    HostKeyMismatchError,
    SSHConnectionError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fingerprints and host-key policy
# ---------------------------------------------------------------------------


def _fingerprint_sha256(key: paramiko.PKey) -> str:
    """Return the SHA256 hex digest of a paramiko ``PKey``.

    The digest is computed over the raw wire encoding of the public key
    (``PKey.asbytes()``), matching what ``ssh-keyscan`` and OpenSSH's
    ``ssh-keygen -lf -E sha256`` produce (modulo encoding — OpenSSH prints
    base64, we use hex for easier DB storage).
    """
    return hashlib.sha256(key.asbytes()).hexdigest()


class _PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Accept the host key iff its SHA256 matches ``expected_pin``.

    If ``expected_pin`` is ``None``, accept any key and record it on
    ``self.observed_pin`` so the caller can persist it (trust on first use).
    """

    def __init__(self, expected_pin: Optional[str]) -> None:
        self.expected_pin = expected_pin
        self.observed_pin: Optional[str] = None

    def missing_host_key(
        self,
        client: paramiko.SSHClient,
        hostname: str,
        key: paramiko.PKey,
    ) -> None:
        actual = _fingerprint_sha256(key)
        self.observed_pin = actual
        if self.expected_pin is None:
            # TOFU: accept and let caller persist.
            logger.debug("accepting first host key for %s", hostname)
            return
        if actual.lower() != self.expected_pin.lower():
            # Do NOT log fingerprints at INFO — they can leak.
            logger.debug(
                "host key mismatch for %s (expected != actual)", hostname
            )
            raise HostKeyMismatchError(self.expected_pin, actual, hostname)


# ---------------------------------------------------------------------------
# Pool internals
# ---------------------------------------------------------------------------


@dataclass
class _PooledClient:
    client: paramiko.SSHClient
    server_id: str
    observed_pin: Optional[str] = None


class SSHPool:
    """Per-process pool of paramiko ``SSHClient`` objects keyed by server id."""

    def __init__(self) -> None:
        self._clients: dict[str, _PooledClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Guards the _locks dict itself (creating per-server locks).
        self._dict_lock = asyncio.Lock()

    async def _lock_for(self, server_id: str) -> asyncio.Lock:
        async with self._dict_lock:
            lock = self._locks.get(server_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[server_id] = lock
            return lock

    # -- connect (sync, runs in a worker thread) ----------------------------

    def _connect_sync(self, server: Server) -> tuple[paramiko.SSHClient, Optional[str]]:
        """Open a fresh ``SSHClient`` for ``server``.

        Returns the connected client plus the observed SHA256 host-key
        fingerprint (so a TOFU caller can persist it).
        """
        client = paramiko.SSHClient()
        policy = _PinnedHostKeyPolicy(server.host_key_pin)
        client.set_missing_host_key_policy(policy)

        kwargs: dict[str, object] = {
            "hostname": server.host,
            "port": server.ssh_port,
            "username": server.ssh_user,
            "timeout": 10,
            "banner_timeout": 10,
            "auth_timeout": 10,
            "allow_agent": False,
            "look_for_keys": False,
        }

        if server.ssh_auth == "pubkey":
            kwargs["pkey"] = _load_pkey(server.ssh_secret_ref)
        elif server.ssh_auth == "password":
            try:
                pw = fernet_decrypt(_read_secret_bytes(server.ssh_secret_ref))
            except Exception as exc:  # noqa: BLE001 — wrap & rethrow
                raise AuthenticationError(
                    "could not decrypt stored password"
                ) from exc
            kwargs["password"] = pw
        else:
            raise AuthenticationError(f"unknown auth method: {server.ssh_auth!r}")

        try:
            client.connect(**kwargs)
        except HostKeyMismatchError:
            # Raised from inside our policy — propagate as-is.
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        except paramiko.AuthenticationException as exc:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            raise AuthenticationError(str(exc) or "authentication failed") from exc
        except (socket.error, paramiko.SSHException) as exc:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            # paramiko sometimes wraps our HostKeyMismatchError; unwrap.
            cause = exc.__cause__
            if isinstance(cause, HostKeyMismatchError):
                raise cause from None
            raise SSHConnectionError(str(exc) or exc.__class__.__name__) from exc
        finally:
            # Scrub the password from local kwargs ASAP — don't keep it
            # in a frame that may end up in a traceback.
            kwargs.pop("password", None)

        return client, policy.observed_pin

    # -- public API ----------------------------------------------------------

    async def acquire(self, server: Server) -> paramiko.SSHClient:
        """Acquire (and hold the per-server lock for) an ``SSHClient``.

        The caller MUST eventually call :meth:`release` (or use
        :meth:`session`, which does it automatically). Concurrent acquires
        for the same server id serialize on the per-server lock so they share
        the underlying transport.
        """
        sid = str(server.id)
        lock = await self._lock_for(sid)
        await lock.acquire()
        try:
            existing = self._clients.get(sid)
            if existing is not None:
                transport = existing.client.get_transport()
                if transport is not None and transport.is_active():
                    return existing.client
                # Stale — close and fall through to reconnect.
                logger.debug("stale transport for server %s; reconnecting", sid)
                try:
                    existing.client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._clients.pop(sid, None)

            client, observed = await asyncio.to_thread(self._connect_sync, server)
            self._clients[sid] = _PooledClient(
                client=client, server_id=sid, observed_pin=observed
            )
            return client
        except BaseException:
            # If anything went wrong, release the lock so we don't deadlock
            # future callers.
            lock.release()
            raise

    async def release(self, server: Server) -> None:
        """Release the per-server lock held by :meth:`acquire`."""
        lock = self._locks.get(str(server.id))
        if lock is not None and lock.locked():
            lock.release()

    @asynccontextmanager
    async def session(
        self, server: Server
    ) -> AsyncIterator[paramiko.SSHClient]:
        """Async context manager: acquire on enter, release on exit."""
        client = await self.acquire(server)
        try:
            yield client
        finally:
            await self.release(server)

    def observed_pin(self, server: Server) -> Optional[str]:
        """Return the SHA256 fingerprint observed at connect time, if any.

        Useful for the "add server" flow: connect with ``host_key_pin=None``,
        then read this back and persist it.
        """
        entry = self._clients.get(str(server.id))
        return entry.observed_pin if entry is not None else None

    async def close(self, server: Server) -> None:
        """Close and forget the transport for a single server."""
        sid = str(server.id)
        lock = await self._lock_for(sid)
        async with lock:
            entry = self._clients.pop(sid, None)
            if entry is not None:
                try:
                    entry.client.close()
                except Exception:  # noqa: BLE001
                    logger.debug("error closing client for %s", sid, exc_info=True)

    async def close_all(self) -> None:
        """Close every pooled transport. Call from app shutdown."""
        async with self._dict_lock:
            entries = list(self._clients.values())
            self._clients.clear()
        for entry in entries:
            try:
                entry.client.close()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "error closing client for %s", entry.server_id, exc_info=True
                )


# ---------------------------------------------------------------------------
# First-connect helper
# ---------------------------------------------------------------------------


async def fingerprint(host: str, port: int = 22, timeout: float = 10.0) -> str:
    """Read the remote host key SHA256 without committing to a session.

    Used by the "add server" admin flow: the operator types in host/port,
    we call this, and present the fingerprint for confirmation before any
    credentials are stored.
    """

    def _fp() -> str:
        sock = socket.create_connection((host, port), timeout=timeout)
        transport: Optional[paramiko.Transport] = None
        try:
            transport = paramiko.Transport(sock)
            transport.start_client(timeout=timeout)
            key = transport.get_remote_server_key()
            return _fingerprint_sha256(key)
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

    try:
        return await asyncio.to_thread(_fp)
    except (socket.error, paramiko.SSHException) as exc:
        raise SSHConnectionError(str(exc) or exc.__class__.__name__) from exc


# ---------------------------------------------------------------------------
# Private-key loading
# ---------------------------------------------------------------------------


def _load_pkey(path: str) -> paramiko.PKey:
    """Try RSA, Ed25519, ECDSA, then DSA. Raise ``AuthenticationError`` on failure.

    Paramiko's ``from_private_key_file`` raises ``SSHException`` when the file
    isn't the expected key type, so we just walk the loaders and accept the
    first one that parses.
    """
    last_exc: Optional[Exception] = None
    for loader in (
        paramiko.Ed25519Key.from_private_key_file,
        paramiko.ECDSAKey.from_private_key_file,
        paramiko.RSAKey.from_private_key_file,
        paramiko.DSSKey.from_private_key_file,
    ):
        try:
            return loader(path)
        except FileNotFoundError as exc:
            raise AuthenticationError(f"private key not found: {path}") from exc
        except PermissionError as exc:
            raise AuthenticationError(
                f"permission denied reading private key: {path}"
            ) from exc
        except paramiko.SSHException as exc:
            last_exc = exc
            continue
    raise AuthenticationError(
        f"could not load private key at {path}"
    ) from last_exc


def _read_secret_bytes(ref: str) -> bytes:
    """Decode the stored ``ssh_secret_ref`` for password auth.

    The ref is stored as ASCII text in the DB (``Text`` column). It's the
    url-safe base64 Fernet ciphertext as produced by
    :func:`app.security.crypto.encrypt` (which returns ``bytes`` — those bytes
    are ASCII-decoded on the way in). We pass the raw ciphertext through
    untouched: ``Fernet.decrypt`` accepts either ``str`` or ``bytes`` of the
    base64 token, so we just return the bytes form.
    """
    # If someone stored the ciphertext already-decoded, that's a bug — but
    # tolerate either ASCII text or already-bytes-like input by re-encoding.
    if isinstance(ref, bytes):
        return ref
    try:
        # Validate by attempting a decode; we don't actually need the raw bytes
        # because Fernet handles base64 itself. We just want a friendly error
        # if the column got corrupted.
        base64.urlsafe_b64decode(ref.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise AuthenticationError(
            "stored password ciphertext is not valid base64"
        ) from exc
    return ref.encode("ascii")


# Module-level singleton, imported as ``from app.services.ssh import ssh_pool``.
ssh_pool = SSHPool()
