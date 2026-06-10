"""Local market-data WS feed client (#110) — consume the sidecar's buy-queue push.

Connects to the per-host market-data service's local fan-out push
(``ws://<MARKET_DATA_URL host>/ws/queue?isin=<ISIN>``) and invokes
``on_update(isin, buy_volume)`` for each frame. The market-data service holds the
single upstream Khobregan RLC WebSocket and re-broadcasts ``{isin, buy_volume}``.

Fail-safe: on disconnect / parse error it calls ``on_update(isin, None)`` so the
auto-sell monitor HOLDs (never sells on a dead or stale feed). Reconnects with
exponential backoff. One daemon thread per ISIN; uses ``websocket-client`` (sync).

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def ws_base(http_url: str) -> str:
    """``http://h:p`` → ``ws://h:p`` (``https`` → ``wss``); trailing slash stripped."""
    u = (http_url or "").rstrip("/")
    if u.startswith("https://"):
        return "wss://" + u[len("https://"):]
    if u.startswith("http://"):
        return "ws://" + u[len("http://"):]
    if u.startswith(("ws://", "wss://")):
        return u
    return "ws://" + u


def parse_buy_volume(message: str) -> Optional[int]:
    """Extract ``buy_volume`` from one JSON push frame (None on absent/garbage)."""
    try:
        obj = json.loads(message)
    except (TypeError, ValueError):
        return None
    bv = obj.get("buy_volume") if isinstance(obj, dict) else None
    if bv is None:
        return None
    try:
        return int(bv)
    except (TypeError, ValueError):
        return None


class QueueFeed:
    """Subscribe to ``/ws/queue?isin=`` per ISIN; push updates to ``on_update``."""

    def __init__(
        self,
        base_url: str,
        on_update: Callable[[str, Optional[int]], None],
        *,
        reconnect_min: float = 1.0,
        reconnect_max: float = 30.0,
        recv_timeout: float = 35.0,
    ):
        self._ws_base = ws_base(base_url)
        self._on_update = on_update
        self._isins: list[str] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self._recv_timeout = recv_timeout

    def subscribe(self, isin: str) -> None:
        if isin not in self._isins:
            self._isins.append(isin)

    def _run_one(self, isin: str) -> None:
        import websocket  # websocket-client

        url = f"{self._ws_base}/ws/queue?isin={isin}"
        backoff = self._reconnect_min
        while not self._stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(url, timeout=15)
                backoff = self._reconnect_min
                ws.settimeout(self._recv_timeout)
                while not self._stop.is_set():
                    msg = ws.recv()
                    if not msg:
                        continue
                    bv = parse_buy_volume(msg)
                    # Only forward real values. A keepalive / non-data frame
                    # (buy_volume absent) is NOT a feed loss — the disconnect
                    # branch below is what signals HOLD via on_update(isin, None).
                    if bv is not None:
                        self._on_update(isin, bv)
            except Exception as exc:  # noqa: BLE001 — disconnect / timeout → HOLD + reconnect
                logger.warning("queue feed %s disconnected: %s", isin, exc)
                self._on_update(isin, None)  # fail-safe HOLD
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self._reconnect_max)
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:  # noqa: BLE001
                    pass

    def start(self) -> None:
        """Spawn one daemon reader thread per subscribed ISIN and RETURN.

        Lets a caller (the monitor's supervisor) own the lifecycle — start a
        feed, keep a handle, and ``stop()`` it on an ISIN-set change without
        blocking. Idempotent-ish: only spawns threads for not-yet-started ISINs.
        """
        started = {t.name for t in self._threads}
        for isin in self._isins:
            name = f"qfeed-{isin}"
            if name in started:
                continue
            t = threading.Thread(target=self._run_one, args=(isin,), daemon=True,
                                 name=name)
            t.start()
            self._threads.append(t)

    def run_forever(self) -> None:
        self.start()
        while not self._stop.is_set():
            time.sleep(1)

    def stop(self) -> None:
        self._stop.set()


__all__ = ["QueueFeed", "ws_base", "parse_buy_volume"]
