"""Paramiko transport pool with host-key pinning and async-friendly wrappers.

Design overview
---------------
Paramiko is a synchronous library. To keep the FastAPI event loop responsive,
every blocking call (``connect``, ``exec_command``, SFTP I/O) is dispatched
through :func:`asyncio.to_thread`. Around that, we layer:

* **One transport per ``(host, port, user)``** — the transport cache is keyed
  by the connection-identity tuple so two ``Server`` rows pointing at the same
  physical endpoint share a single TCP connection, and a credential rotation
  on a server record forces a reconnect. A separate per-connection-identity
  ``asyncio.Lock`` serialises concurrent acquires.
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


def _conn_key(server: Server) -> str:
    """Return the connection-identity key for ``server``.

    Two ``Server`` rows with the same ``(ssh_user, host, ssh_port)`` share a
    transport. Changing any of these fields on a row produces a new key, so
    the next :meth:`SSHPool.acquire` will open a fresh connection rather than
    reuse a stale one against the old endpoint.
    """
    return f"{server.ssh_user}@{server.host}:{server.ssh_port}"


def _secret_fingerprint(server: Server) -> str:
    """Return a stable short digest of the auth-relevant fields.

    Used to detect a "same conn_key, but credentials rotated" case: an admin
    updates ``ssh_secret_ref`` (e.g. a password rotation) or pins a new
    ``host_key_pin`` without changing host/port/user. The digest is short and
    hashed so the raw secret reference never appears in any cache key or log.
    """
    h = hashlib.sha256()
    h.update((server.ssh_auth or "").encode("utf-8"))
    h.update(b"\0")
    h.update((server.ssh_secret_ref or "").encode("utf-8"))
    h.update(b"\0")
    h.update((server.host_key_pin or "").encode("utf-8"))
    return h.hexdigest()[:16]


@dataclass
class _PooledClient:
    client: paramiko.SSHClient
    conn_key: str
    secret_fingerprint: str
    observed_pin: Optional[str] = None


class SSHPool:
    """Per-process pool of paramiko ``SSHClient`` objects.

    Keyed by :func:`_conn_key` (``user@host:port``) rather than ``server.id`` so
    that changing a server row's host/port/user — or its stored secret —
    correctly triggers a reconnect on the next acquire instead of returning a
    stale authenticated transport.
    """

    def __init__(self) -> None:
        self._clients: dict[str, _PooledClient] = {}
        self._conn_locks: dict[str, asyncio.Lock] = {}
        # Guards the _conn_locks dict itself (creating per-conn locks).
        self._dict_lock = asyncio.Lock()

    async def _lock_for_conn(self, conn_key: str) -> asyncio.Lock:
        async with self._dict_lock:
            lock = self._conn_locks.get(conn_key)
            if lock is None:
                lock = asyncio.Lock()
                self._conn_locks[conn_key] = lock
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
        """Return an ``SSHClient`` for ``server`` (reused if alive, else fresh).

        Concurrent acquires for the same ``(user, host, port)`` serialize on
        the per-connection lock JUST LONG ENOUGH to decide reuse-vs-reconnect
        and to mutate the ``_clients`` dict. **The lock is released before
        the client is returned** — multiple concurrent callers then share the
        same SSHClient and open independent paramiko Channels on it (which
        is what SSH multiplexing is for).

        Holding the lock for the lifetime of a session (as the previous
        implementation did) serialised entire `docker exec` / `docker logs`
        operations across stacks that happened to point at the same host —
        observable as agent B's run row sitting at ``status='running'`` while
        agent A's run held the lock for the whole 120 s locust race. See
        issue #66.

        If the cached transport's stored ``secret_fingerprint`` no longer
        matches the current ``server`` row (admin rotated the password or
        pinned a new host key without changing host/port/user), the cached
        client is closed and a fresh connection is established.
        """
        conn_key = _conn_key(server)
        lock = await self._lock_for_conn(conn_key)
        async with lock:
            current_fp = _secret_fingerprint(server)
            existing = self._clients.get(conn_key)
            if existing is not None:
                transport = existing.client.get_transport()
                alive = transport is not None and transport.is_active()
                if alive and existing.secret_fingerprint == current_fp:
                    return existing.client
                if not alive:
                    logger.debug(
                        "stale transport for %s; reconnecting", conn_key
                    )
                else:
                    # Credentials/host pin rotated for this same conn_key.
                    logger.debug(
                        "credentials drifted for %s; reconnecting", conn_key
                    )
                try:
                    existing.client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._clients.pop(conn_key, None)

            client, observed = await asyncio.to_thread(self._connect_sync, server)
            self._clients[conn_key] = _PooledClient(
                client=client,
                conn_key=conn_key,
                secret_fingerprint=current_fp,
                observed_pin=observed,
            )
            return client

    async def release(self, server: Server) -> None:
        """Backwards-compatibility shim — releases nothing.

        The pool no longer holds a per-connection lock across the session
        lifetime; the lock is dropped inside :meth:`acquire` once the
        connect-or-reuse decision is committed. Existing call sites that
        invoke ``release`` (via :meth:`session`'s ``finally`` or directly)
        still work — this is just a no-op now. Kept rather than removed so
        third-party code calling the documented :meth:`acquire` /
        :meth:`release` pair doesn't break.
        """
        return None

    @asynccontextmanager
    async def session(
        self, server: Server
    ) -> AsyncIterator[paramiko.SSHClient]:
        """Async context manager: acquire on enter, release on exit.

        Note: after the fix for issue #66, "release on exit" is a no-op.
        The pool's locking is now only around the connect-or-reuse decision
        inside :meth:`acquire`; concurrent sessions on the same SSHClient
        are explicitly supported and open independent paramiko Channels.
        """
        client = await self.acquire(server)
        try:
            yield client
        finally:
            await self.release(server)

    async def run_with_retry(
        self,
        server: Server,
        sync_work,  # Callable[[paramiko.SSHClient], T]
        *,
        retries: int = 1,
    ):
        """Run ``sync_work(client)`` in a worker thread with stale-transport recovery.

        The pool's :meth:`acquire` only checks ``transport.is_active()``, which
        keeps returning ``True`` for a transport whose underlying socket has
        gone away (NAT timeout, remote sshd restart, idle disconnect) until
        the next channel-open round-trip actually fails. When that happens
        paramiko raises :class:`paramiko.ChannelException` (or in some races a
        bare :class:`paramiko.SSHException`) — but the broken client stays in
        the pool, so every subsequent caller hits the same wall.

        This wrapper turns the broken-transport case into a self-healing
        single retry: if ``sync_work`` raises a transport-level failure on
        the first attempt, we evict the cached client and re-acquire (which
        forces a fresh transport), then run ``sync_work`` again. Auth and
        host-key failures are NOT retried — those are caller errors, not
        transport-decay, and retrying would spam credential attempts.

        See issue #94 for the user-visible symptom this fixes.
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(retries + 1):
            client = await self.acquire(server)
            try:
                return await asyncio.to_thread(sync_work, client)
            except (
                paramiko.AuthenticationException,
                paramiko.BadHostKeyException,
            ):
                # Caller-input failures; don't retry.
                raise
            except (paramiko.ChannelException, paramiko.SSHException) as exc:
                last_exc = exc
                if attempt >= retries:
                    raise
                logger.warning(
                    "ssh op failed on %s (attempt %d/%d); evicting and retrying: %s",
                    _conn_key(server), attempt + 1, retries + 1, exc,
                )
                await self.close(server)
        # Unreachable — the loop either returns or re-raises — but keep the
        # type-checker happy.
        assert last_exc is not None
        raise last_exc

    def observed_pin(self, server: Server) -> Optional[str]:
        """Return the SHA256 fingerprint observed at connect time, if any.

        Useful for the "add server" flow: connect with ``host_key_pin=None``,
        then read this back and persist it.
        """
        entry = self._clients.get(_conn_key(server))
        return entry.observed_pin if entry is not None else None

    async def close(self, server: Server) -> None:
        """Force-close and forget the transport for this connection identity."""
        conn_key = _conn_key(server)
        lock = await self._lock_for_conn(conn_key)
        async with lock:
            entry = self._clients.pop(conn_key, None)
            if entry is not None:
                try:
                    entry.client.close()
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "error closing client for %s", conn_key, exc_info=True
                    )

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
                    "error closing client for %s", entry.conn_key, exc_info=True
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
