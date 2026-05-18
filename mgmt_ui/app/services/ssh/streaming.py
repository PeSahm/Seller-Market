"""Stream stdout/stderr lines from a long-running remote command over SSH.

Used by the run executor (`docker exec ...`) and the WS log streamer
(`docker logs -f ...`). Returns an async iterator of lines so the caller
can both forward to a WebSocket AND capture for the archive.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import socket
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import paramiko

# Aliased so the inner `_blocking` closure can reference it cleanly
# without the surrounding scope colliding with any future `shlex` use.
shlex_quote = shlex.quote

from app.models.servers import Server
from app.services.ssh.exceptions import SSHConnectionError, SSHError
from app.services.ssh.pool import ssh_pool

logger = logging.getLogger(__name__)


@dataclass
class StreamingResult:
    """Final state returned after the remote command exits."""

    exit_code: Optional[int]
    captured: bytes  # full stdout+stderr concatenated, for the archive


@dataclass
class LineEvent:
    """One line of output, tagged with source so callers can prefix or colour."""

    stream: str  # "stdout" or "stderr"
    data: bytes  # raw bytes, NOT decoded (so binary-ish output doesn't crash)


async def stream_remote_command(
    server: Server,
    command: str,
    *,
    timeout: float = 1800.0,  # 30-minute hard ceiling for safety
) -> AsyncIterator[object]:
    """Run ``command`` on ``server``, yielding ``LineEvent`` per line as they arrive.

    The final yield is a :class:`StreamingResult` — callers use ``isinstance``
    to spot it.

    Errors:
        SSHConnectionError on dial / channel failures
        SSHError on unexpected paramiko exceptions

    The yielded ``captured`` bytes in :class:`StreamingResult` is the FULL
    output even if the consumer breaks out early — useful for archiving.
    """
    captured = bytearray()
    queue: asyncio.Queue = asyncio.Queue()
    DONE = object()
    loop = asyncio.get_event_loop()
    # Single-element bucket so the asyncio-side timeout handler can grab
    # the paramiko Channel that the thread opened and close it from
    # outside. Lists are thread-safe for the append-then-read access we
    # do here.
    chan_ref: list = []

    async def _pump() -> None:
        """Drive the SSH channel in a worker thread; push events into queue."""
        try:
            async with ssh_pool.session(server) as client:

                def _blocking() -> int:
                    """Run inside ``asyncio.to_thread`` — owns the channel."""
                    transport = client.get_transport()
                    if transport is None or not transport.is_active():
                        raise SSHConnectionError("ssh transport not active")
                    chan = transport.open_session()
                    try:
                        # Expose the channel to the outer scope so the
                        # asyncio-side timeout handler can forcibly close
                        # it (which signals SSH_MSG_CHANNEL_CLOSE → docker
                        # exec EOF → in-container process killed). See PR
                        # #49 review #5.
                        chan_ref.append(chan)
                        chan.settimeout(1.0)  # non-blocking-ish read cadence
                        # Wrap the user-supplied command in the GNU
                        # `timeout` utility (available on every supported
                        # trading server image) as a belt-and-suspenders:
                        # if our asyncio-side close somehow fails to kill
                        # the remote (very unlikely with docker exec but
                        # has bitten us before with backgrounded children),
                        # the remote `timeout` SIGTERMs after the same
                        # window, then SIGKILLs 5 s later. The extra 30 s
                        # buffer means our local timeout fires first under
                        # normal conditions.
                        remote_timeout = int(timeout) + 30
                        wrapped = (
                            f"timeout --kill-after=5 {remote_timeout} sh -c "
                            f"{shlex_quote(command)}"
                        )
                        chan.exec_command(wrapped)
                        stdout_buf = bytearray()
                        stderr_buf = bytearray()
                        # We loop until channel reports exit AND drains.
                        while True:
                            advanced = False
                            if chan.recv_ready():
                                chunk = chan.recv(8192)
                                if chunk:
                                    stdout_buf.extend(chunk)
                                    captured.extend(chunk)
                                    advanced = True
                                    # Split full lines off the buffer.
                                    while b"\n" in stdout_buf:
                                        line, _, rest = stdout_buf.partition(b"\n")
                                        asyncio.run_coroutine_threadsafe(
                                            queue.put(LineEvent("stdout", bytes(line))),
                                            loop,
                                        )
                                        stdout_buf = bytearray(rest)
                            if chan.recv_stderr_ready():
                                chunk = chan.recv_stderr(8192)
                                if chunk:
                                    stderr_buf.extend(chunk)
                                    captured.extend(chunk)
                                    advanced = True
                                    while b"\n" in stderr_buf:
                                        line, _, rest = stderr_buf.partition(b"\n")
                                        asyncio.run_coroutine_threadsafe(
                                            queue.put(LineEvent("stderr", bytes(line))),
                                            loop,
                                        )
                                        stderr_buf = bytearray(rest)
                            if chan.exit_status_ready() and not (
                                chan.recv_ready() or chan.recv_stderr_ready()
                            ):
                                # Flush any trailing partial line.
                                if stdout_buf:
                                    asyncio.run_coroutine_threadsafe(
                                        queue.put(LineEvent("stdout", bytes(stdout_buf))),
                                        loop,
                                    )
                                if stderr_buf:
                                    asyncio.run_coroutine_threadsafe(
                                        queue.put(LineEvent("stderr", bytes(stderr_buf))),
                                        loop,
                                    )
                                return chan.recv_exit_status()
                            if not advanced:
                                # Tiny sleep so we don't burn CPU when idle.
                                # The channel timeout itself is 1s so this is
                                # belt-and-braces.
                                time.sleep(0.05)
                    finally:
                        try:
                            chan.close()
                        except Exception:  # noqa: BLE001
                            pass

                try:
                    rc = await asyncio.wait_for(
                        asyncio.to_thread(_blocking), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    # asyncio.wait_for cancels the inner coroutine but
                    # CANNOT kill the thread that asyncio.to_thread
                    # spawned. We have to close the SSH channel ourselves
                    # to actually terminate the remote process — without
                    # this, docker exec would keep running on the trading
                    # server even though we've already finalized the run
                    # as failed. See PR #49 review #5.
                    if chan_ref:
                        try:
                            chan_ref[0].close()
                        except Exception:  # noqa: BLE001
                            pass
                    await queue.put(
                        LineEvent(
                            "stderr",
                            f"<run-executor> timeout exceeded ({timeout}s); "
                            f"channel closed → remote SIGHUP".encode(),
                        )
                    )
                    rc = -1
                await queue.put(StreamingResult(exit_code=rc, captured=bytes(captured)))
        except paramiko.SSHException as exc:
            await queue.put(LineEvent("stderr", f"<ssh-error> {exc}".encode()))
            await queue.put(StreamingResult(exit_code=None, captured=bytes(captured)))
        except (socket.error, OSError) as exc:
            await queue.put(LineEvent("stderr", f"<net-error> {exc}".encode()))
            await queue.put(StreamingResult(exit_code=None, captured=bytes(captured)))
        except SSHError as exc:
            await queue.put(LineEvent("stderr", f"<ssh-error> {exc}".encode()))
            await queue.put(StreamingResult(exit_code=None, captured=bytes(captured)))
        finally:
            await queue.put(DONE)

    pump_task = asyncio.create_task(_pump())
    try:
        while True:
            item = await queue.get()
            if item is DONE:
                return
            yield item  # LineEvent or StreamingResult
    finally:
        # On generator close (including cancellation from a terminate-run
        # button click upstream), explicitly close the paramiko channel so
        # the SSH server sends CHANNEL_CLOSE → docker exec sees EOF → the
        # in-container process is killed. Without this, the `_blocking`
        # thread keeps reading from a still-open channel and the remote
        # python (e.g. cache_warmup retry loop) outlives the asyncio task.
        if chan_ref:
            try:
                chan_ref[0].close()
            except Exception:  # noqa: BLE001
                pass
        if not pump_task.done():
            pump_task.cancel()


__all__ = ["LineEvent", "StreamingResult", "stream_remote_command"]
