"""Regression tests for issue #94 — SSH-pool stale-transport recovery.

The pool keeps a single ``paramiko.SSHClient`` per ``(user, host, port)``
and reuses it as long as ``transport.is_active()`` returns True. That
flag is too coarse: a transport whose socket has gone away can keep
returning True until the next channel-open round-trip fails with
``ChannelException(2, 'Connect failed')``. Before this fix the pool kept
serving the broken client to every subsequent caller, producing thousands
of paramiko log lines and a UI flash for the user.

``SSHPool.run_with_retry`` runs the supplied sync work in a worker thread,
catches transport-level failures, evicts the cached client, and retries
once on a fresh transport. These tests pin the three branches:

* ``test_retries_once_on_channel_exception`` — fail → evict → succeed.
* ``test_does_not_retry_on_auth_error`` — credential errors propagate
  immediately (no spam, no eviction).
* ``test_reraises_after_exhausted_retries`` — second failure surfaces the
  original paramiko exception so callers can map it to their own error.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import paramiko
import pytest

from app.services.ssh.pool import SSHPool


def _fake_server() -> SimpleNamespace:
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        host="10.0.0.1",
        ssh_port=22,
        ssh_user="root",
        ssh_auth="password",
        ssh_secret_ref="encrypted-blob",
        host_key_pin=None,
    )


def _make_live_client(name: str = "SSHClient") -> MagicMock:
    client = MagicMock(name=name)
    transport = MagicMock(name=f"{name}-Transport")
    transport.is_active.return_value = True
    client.get_transport.return_value = transport
    return client


@pytest.mark.asyncio
async def test_retries_once_on_channel_exception() -> None:
    """A ChannelException on the first attempt evicts the cached client
    and the retry runs against a freshly-connected one."""
    pool = SSHPool()
    server = _fake_server()
    stale_client = _make_live_client("stale")
    fresh_client = _make_live_client("fresh")

    # First connect returns the stale client; after eviction, the second
    # connect returns the fresh one.
    connect_results = iter([
        (stale_client, "pin"),
        (fresh_client, "pin"),
    ])

    def _fake_connect(_srv):
        return next(connect_results)

    attempts: list[MagicMock] = []

    def _sync_work(client: MagicMock) -> str:
        attempts.append(client)
        if client is stale_client:
            raise paramiko.ChannelException(2, "Connect failed")
        return "ok"

    with patch.object(pool, "_connect_sync", side_effect=_fake_connect):
        result = await pool.run_with_retry(server, _sync_work)

    assert result == "ok"
    assert attempts == [stale_client, fresh_client]
    # The stale client must have been evicted (close() called) so a future
    # acquire reconnects rather than serving it again.
    stale_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_does_not_retry_on_auth_error() -> None:
    """AuthenticationException is a caller-input failure, not a stale
    transport. Retrying would just spam another failed credential attempt."""
    pool = SSHPool()
    server = _fake_server()
    client = _make_live_client()

    def _fake_connect(_srv):
        return client, "pin"

    call_count = 0

    def _sync_work(_client: MagicMock) -> None:
        nonlocal call_count
        call_count += 1
        raise paramiko.AuthenticationException("bad password")

    with patch.object(pool, "_connect_sync", side_effect=_fake_connect):
        with pytest.raises(paramiko.AuthenticationException):
            await pool.run_with_retry(server, _sync_work)

    # Exactly one call — no retry.
    assert call_count == 1
    # And the client was NOT evicted: auth failures don't indicate the
    # transport is broken, and the next acquire's credentials might be
    # rotated independently anyway.
    client.close.assert_not_called()


@pytest.mark.asyncio
async def test_reraises_after_exhausted_retries() -> None:
    """If both the first attempt AND the retry fail with a transport-level
    error, the original exception surfaces (callers convert to their app
    exception themselves)."""
    pool = SSHPool()
    server = _fake_server()

    def _fake_connect(_srv):
        return _make_live_client(), "pin"

    def _always_fail(_client: MagicMock) -> None:
        raise paramiko.ChannelException(2, "Connect failed")

    with patch.object(pool, "_connect_sync", side_effect=_fake_connect):
        with pytest.raises(paramiko.ChannelException):
            await pool.run_with_retry(server, _always_fail)
