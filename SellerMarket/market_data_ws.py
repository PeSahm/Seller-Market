"""Local market-data WS feed client (#110) — consume the sidecar's buy-queue push.

Connects to the per-host market-data service's local fan-out push
(``ws://<MARKET_DATA_URL host>/ws/queue?isin=<ISIN>``) and invokes
``on_update(isin, buy_volume)`` for each frame. The market-data service holds the
single upstream Khobregan RLC WebSocket and re-broadcasts ``{isin, buy_volume}``.

``MARKET_DATA_URL`` may be a single URL OR a comma/space-separated **failover
pool** (mirrors the OCR pool): the feed tries the endpoints in order, *always
preferring the first*, and only advances to the next when the earlier ones are
unreachable. Prefer-first is deliberate — because every ISIN thread (and every
bot) starts the list at index 0, all subscribers reconverge on the primary
sidecar whenever it is healthy, so exactly ONE sidecar is ever subscribed →
ONE upstream Khobregan WS (the single-account invariant). The pool is for
OUTAGE failover, never simultaneous sharding. So a sidecar going down needs no
redeploy: the bot already carries the backup address and fails over on its own.

Fail-safe: on disconnect / parse error / all-endpoints-unreachable it calls
``on_update(isin, None)`` so the auto-sell monitor HOLDs (never sells on a dead
or stale feed). Reconnects with exponential backoff. One daemon thread per ISIN;
uses ``websocket-client`` (sync).

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


def ws_bases(raw: str) -> list[str]:
    """Parse ``MARKET_DATA_URL`` into an ordered list of ``ws://`` bases.

    Accepts a single URL or a comma/space-separated failover pool; each entry is
    normalised via :func:`ws_base` (trailing slash stripped). A single URL yields
    a one-element list (backward compatible). Order is preserved — index 0 is the
    preferred primary.
    """
    parts = (raw or "").replace(",", " ").split()
    return [ws_base(p) for p in parts if p.strip()]


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
        primary_recheck: float = 45.0,
    ):
        # ``base_url`` may be a single URL or a comma/space-separated failover
        # pool. ``_ws_bases`` is the ordered list (index 0 = preferred primary).
        self._ws_bases = ws_bases(base_url)
        self._on_update = on_update
        self._isins: list[str] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self._recv_timeout = recv_timeout
        # When connected to a NON-primary endpoint, drop + re-attempt the primary
        # every ``primary_recheck`` seconds. WITHOUT this, a healthy backup never
        # disconnects (the sidecar sends a keepalive every ~30s, so recv never
        # times out) and the thread would stick on the backup forever — splitting
        # the fleet across two sidecars = two upstream Khobregan logins on one
        # account. The recheck forces reconvergence on the primary once it
        # recovers (within ``primary_recheck`` s), restoring the single upstream.
        self._primary_recheck = primary_recheck

    def subscribe(self, isin: str) -> None:
        if isin not in self._isins:
            self._isins.append(isin)

    def _run_one(self, isin: str) -> None:
        import websocket  # websocket-client

        bases = self._ws_bases
        backoff = self._reconnect_min
        while not self._stop.is_set():
            ws = None
            used_base = None
            # --- connect phase: try each endpoint in order (prefer-primary) ---
            # The first that connects wins; a later one is used ONLY when the
            # earlier ones are unreachable. We do NOT signal HOLD between
            # endpoints — only once the WHOLE list has failed (below), so a
            # healthy backup is reached without a spurious HOLD on the primary.
            for base in bases:
                if self._stop.is_set():
                    return
                url = f"{base}/ws/queue?isin={isin}"
                try:
                    ws = websocket.create_connection(url, timeout=15)
                    used_base = base
                    break
                except Exception as exc:  # noqa: BLE001 — try the next endpoint
                    ws = None
                    logger.warning("queue feed %s connect via %s failed: %s",
                                   isin, base, exc)
            if ws is None:
                # Every endpoint is unreachable → HOLD (fail-safe), then back off
                # before re-trying the whole list from the primary.
                self._on_update(isin, None)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self._reconnect_max)
                continue
            # --- recv phase: a live connection. Reset backoff; pump until drop ---
            backoff = self._reconnect_min
            on_primary = used_base == bases[0]
            # On a non-primary endpoint, set a wall-time deadline to drop the
            # connection and re-attempt the primary. This is wall-time (not the
            # recv timeout) ON PURPOSE: the sidecar's ~30s keepalive keeps recv
            # alive indefinitely, so a recv-timeout-based recheck would never
            # fire on a healthy backup. ``None`` on the primary → stay connected.
            deadline = None if on_primary else (time.monotonic() + self._primary_recheck)
            recheck = False
            disconnected = False
            if len(bases) > 1:
                logger.info("queue feed %s connected via %s", isin, used_base)
            try:
                ws.settimeout(self._recv_timeout)
                while not self._stop.is_set():
                    if deadline is not None and time.monotonic() >= deadline:
                        recheck = True   # time to re-attempt the primary
                        break
                    msg = ws.recv()
                    if not msg:
                        continue
                    bv = parse_buy_volume(msg)
                    # Only forward real values. A keepalive / non-data frame
                    # (buy_volume absent) is NOT a feed loss — the disconnect
                    # branch below is what signals HOLD via on_update(isin, None).
                    if bv is not None:
                        self._on_update(isin, bv)
            except Exception as exc:  # noqa: BLE001 — disconnect / timeout → HOLD
                logger.warning("queue feed %s disconnected: %s", isin, exc)
                self._on_update(isin, None)  # fail-safe HOLD
                disconnected = True
            finally:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
            if recheck:
                # Planned drop to reclaim the primary — NOT a failure, so don't
                # grow backoff; HOLD the brief gap (fail-safe) and reconnect from
                # the primary promptly.
                logger.info("queue feed %s rechecking primary (was on %s)",
                            isin, used_base)
                self._on_update(isin, None)
                self._stop.wait(self._reconnect_min)
            elif disconnected:
                # Established connection dropped: wait (backoff == reconnect_min
                # here, just reset on connect — byte-identical to the single-URL
                # path before this change), then grow so a flaky accept-then-drop
                # endpoint backs off on the next all-unreachable cycle.
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self._reconnect_max)

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


__all__ = ["QueueFeed", "ws_base", "ws_bases", "parse_buy_volume"]
