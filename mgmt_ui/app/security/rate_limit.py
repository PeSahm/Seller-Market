"""In-process token-bucket rate limiter.

Phase 10 hardening — protects auth endpoints from brute-force and
ws-token endpoints from cheap DoS. Per-client buckets keyed by IP
(``request.client.host``); a single dict guarded by an asyncio.Lock
for thread safety in the single-process uvicorn worker.

Limitations: in-process only. Behind a multi-worker / horizontally
scaled deployment, replace with a Redis-backed counter. The interface
(``check_and_consume``) is identical so the swap is contained.

Behind a real proxy (Caddy, nginx, Cloudflare), uvicorn sees the
proxy's IP unless the proxy sets ``X-Forwarded-For`` and uvicorn was
started with ``--forwarded-allow-ips``. The deployment doc covers this.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class _Bucket:
    """Token-bucket state per client.

    ``tokens`` is a float so partial refill works (e.g. 0.5 tokens/s
    means a token every 2 seconds).
    """
    tokens: float
    last_refill: float


class RateLimiter:
    """Token-bucket per-key rate limiter.

    Each call to :meth:`check_and_consume` either:
    - returns True (under the limit, one token consumed), or
    - returns False (limit hit, caller raises 429).

    Tokens refill continuously at ``refill_per_second``. Initial bucket
    is full (``capacity`` tokens), so the first burst is allowed to use
    the full window.

    Buckets are kept indefinitely — for a busy deployment this is a
    bounded memory cost (number of distinct client IPs). A simple
    sweep on a timer could trim, but isn't worth the complexity at
    this scale.
    """

    def __init__(self, *, capacity: int, refill_per_second: float):
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        if refill_per_second <= 0:
            raise ValueError(
                f"refill_per_second must be > 0, got {refill_per_second}"
            )
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def check_and_consume(self, key: str) -> bool:
        """Atomically refill + decrement the bucket for ``key``.

        Returns True iff a token was available (and consumed). The
        caller decides what to do on False — typically raise 429.
        """
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.capacity), last_refill=now)
                self._buckets[key] = bucket
            else:
                # Refill since last touch.
                elapsed = now - bucket.last_refill
                bucket.tokens = min(
                    self.capacity,
                    bucket.tokens + elapsed * self.refill_per_second,
                )
                bucket.last_refill = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

    def reset(self, key: Optional[str] = None) -> None:
        """Test helper. ``None`` clears every bucket."""
        if key is None:
            self._buckets.clear()
        else:
            self._buckets.pop(key, None)


# Module-level singletons for the two endpoints called out in the
# Phase 10 plan. Tuned per the spec:
#
#   /auth/login          — 10 attempts per IP per minute
#   /auth/ws-token       — 60 per minute per IP (run-detail pages
#                          call this on every WS reconnect)
#
# Capacity = burst budget; refill = sustained rate. With capacity=10
# and refill≈1/6 token per second, a client can fire 10 attempts in
# the first second and then ~1 every 6 s until the bucket is full
# again.
login_limiter = RateLimiter(capacity=10, refill_per_second=10 / 60)
ws_token_limiter = RateLimiter(capacity=60, refill_per_second=60 / 60)


def client_key(request) -> str:
    """Pull a stable per-client key out of a Starlette request.

    Falls back to ``"anonymous"`` if the client info is missing (e.g.
    in some test transports). All buckets for anonymous traffic share
    one bucket — fine for unit tests, not a real concern in prod
    because uvicorn always populates ``request.client``.
    """
    if request.client is None:
        return "anonymous"
    return request.client.host or "anonymous"


__all__ = [
    "RateLimiter",
    "client_key",
    "login_limiter",
    "ws_token_limiter",
]
