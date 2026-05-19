"""Regression test for issue #66 — concurrent SSH sessions to the same host.

The previous SSHPool implementation held the per-connection asyncio Lock
for the entire ``async with ssh_pool.session(server)`` block. While agent
A's run was in flight (~120 s during a locust race), agent B's
``_run_executor_loop`` would block on ``lock.acquire()`` inside the pool
— the mgmt UI showed agent B at status='running' but the executor was
stuck deep in the SSH layer. See:
https://github.com/PeSahm/Seller-Market/issues/66

Paramiko transports natively multiplex multiple Channels per TCP
connection, so the lock only needs to guard the connect-or-reuse
decision. The fix in ``app/services/ssh/pool.py::SSHPool.acquire``
releases the lock once the client is committed to the cache.

This test pins the new behaviour by:
  * Patching ``_connect_sync`` so we don't actually open a TCP socket.
  * Holding one session open in an ``asyncio.Event``-gated coroutine.
  * Starting a second session against the SAME server and asserting it
    enters its critical region BEFORE the first releases — which would
    have been impossible under the old lock-across-session model.

Also covers: the second concurrent acquire MUST reuse the cached client
(not reconnect), since reconnecting under contention would silently
serialise again via ``_connect_sync``'s thread call.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.ssh.pool import SSHPool


def _fake_server() -> SimpleNamespace:
    """``Server``-shaped object with the fields ``_conn_key`` /
    ``_secret_fingerprint`` read at acquire time. ssh_auth is irrelevant
    here because ``_connect_sync`` is patched."""
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        host="10.0.0.1",
        ssh_port=22,
        ssh_user="root",
        ssh_auth="password",
        ssh_secret_ref="encrypted-blob",
        host_key_pin=None,
    )


def _make_live_client() -> MagicMock:
    """Mock ``SSHClient`` whose ``get_transport().is_active()`` returns True.

    The pool's reuse path reads ``transport = client.get_transport()`` and
    then ``transport.is_active()`` — both must succeed for the second
    acquire to reuse rather than reconnect.
    """
    client = MagicMock(name="SSHClient")
    transport = MagicMock(name="Transport")
    transport.is_active.return_value = True
    client.get_transport.return_value = transport
    return client


@pytest.mark.asyncio
async def test_concurrent_acquires_do_not_serialize_across_sessions() -> None:
    """Two ``async with ssh_pool.session(server)`` blocks against the same
    server must run concurrently — the second MUST be able to do work
    while the first is still inside its ``with`` block.

    This is the core promise broken by the original implementation that
    held the lock across the whole session.
    """
    pool = SSHPool()
    server = _fake_server()
    cached_client = _make_live_client()

    # ``_connect_sync`` is invoked at most once because the second acquire
    # reuses the cached client. Count calls to prove that.
    connect_calls = 0

    def _fake_connect(_srv):
        nonlocal connect_calls
        connect_calls += 1
        return cached_client, "fake-sha256-pin"

    with patch.object(pool, "_connect_sync", side_effect=_fake_connect):
        # Pre-populate the cache by running one acquire first so the second
        # acquires (running concurrently below) hit the reuse path. The
        # cross-session-concurrency property holds either way; pre-warming
        # just makes the assertions about connect_calls clean.
        async with pool.session(server) as c0:
            assert c0 is cached_client

        # Now race two sessions. Worker A enters first, holds its session
        # open until released, while worker B starts AFTER A's enter and
        # must reach its critical region without waiting for A's exit.
        a_inside = asyncio.Event()
        a_may_exit = asyncio.Event()
        b_inside = asyncio.Event()

        async def worker_a() -> None:
            async with pool.session(server) as c:
                assert c is cached_client
                a_inside.set()
                # Block here — would have starved B under the old code.
                await a_may_exit.wait()

        async def worker_b() -> None:
            # Wait until A is definitively inside its session.
            await a_inside.wait()
            async with pool.session(server) as c:
                assert c is cached_client
                b_inside.set()

        task_a = asyncio.create_task(worker_a())
        task_b = asyncio.create_task(worker_b())

        # Give the scheduler a chance to interleave. Under the bug, B would
        # be parked on lock.acquire() and never reach b_inside.set().
        await asyncio.wait_for(b_inside.wait(), timeout=1.0)

        # Both inside concurrently. Release A.
        a_may_exit.set()
        await asyncio.gather(task_a, task_b)

    # Exactly one _connect_sync call: pre-warm. The two racing acquires
    # both took the reuse path.
    assert connect_calls == 1


@pytest.mark.asyncio
async def test_per_conn_lock_still_serialises_connect_decision() -> None:
    """The lock MUST still serialise the connect-or-reuse decision so two
    cold acquires for the same conn_key don't both run ``_connect_sync``
    and double-up in ``_clients``.
    """
    pool = SSHPool()
    server = _fake_server()
    connect_calls = 0
    barrier_entered = asyncio.Event()
    proceed = asyncio.Event()

    async def _slow_connect(_srv):
        # Use asyncio.sleep so the test stays single-threaded — the real
        # ``_connect_sync`` runs in to_thread but the pool's lock model
        # doesn't care about that.
        nonlocal connect_calls
        connect_calls += 1
        barrier_entered.set()
        await proceed.wait()
        return _make_live_client(), "fake-sha256-pin"

    # Patch ``asyncio.to_thread`` for THIS pool's _connect_sync call
    # so our coroutine-shaped fake gets awaited directly.
    async def _to_thread_passthrough(fn, *args, **kwargs):
        return await _slow_connect(*args, **kwargs)

    with patch("app.services.ssh.pool.asyncio.to_thread", side_effect=_to_thread_passthrough):
        task_a = asyncio.create_task(pool.acquire(server))
        # Wait until task_a is mid-connect.
        await asyncio.wait_for(barrier_entered.wait(), timeout=1.0)

        # Now launch task_b. It MUST block on the per-conn lock (still
        # held by task_a inside its connect block) — connect_calls
        # should remain 1 even if we let the event loop spin.
        task_b = asyncio.create_task(pool.acquire(server))
        # Give task_b plenty of scheduler ticks. If the lock isn't holding
        # it, it would race past and bump connect_calls.
        for _ in range(20):
            await asyncio.sleep(0)
        assert connect_calls == 1

        # Now let task_a finish; task_b takes the reuse path.
        proceed.set()
        client_a = await task_a
        client_b = await task_b
        assert client_a is client_b
        assert connect_calls == 1
