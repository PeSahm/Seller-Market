"""Unit tests for app.security.rate_limit (Phase 10)."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.security.rate_limit import RateLimiter


@pytest.mark.asyncio
async def test_first_call_succeeds():
    limiter = RateLimiter(capacity=3, refill_per_second=1.0)
    assert await limiter.check_and_consume("ip1") is True


@pytest.mark.asyncio
async def test_burst_up_to_capacity_succeeds():
    limiter = RateLimiter(capacity=3, refill_per_second=1.0)
    for _ in range(3):
        assert await limiter.check_and_consume("ip1") is True


@pytest.mark.asyncio
async def test_over_capacity_fails():
    limiter = RateLimiter(capacity=3, refill_per_second=0.001)
    for _ in range(3):
        assert await limiter.check_and_consume("ip1") is True
    assert await limiter.check_and_consume("ip1") is False


@pytest.mark.asyncio
async def test_buckets_are_per_key():
    """ip1 hitting the limit does NOT affect ip2."""
    limiter = RateLimiter(capacity=2, refill_per_second=0.001)
    assert await limiter.check_and_consume("ip1") is True
    assert await limiter.check_and_consume("ip1") is True
    assert await limiter.check_and_consume("ip1") is False
    # Fresh key, full bucket.
    assert await limiter.check_and_consume("ip2") is True


@pytest.mark.asyncio
async def test_refill_restores_capacity(monkeypatch):
    """Simulate elapsed time via monotonic patch."""
    limiter = RateLimiter(capacity=2, refill_per_second=1.0)
    base = time.monotonic()
    monkeypatch.setattr(
        "app.security.rate_limit.time.monotonic", lambda: base
    )
    assert await limiter.check_and_consume("ip1") is True
    assert await limiter.check_and_consume("ip1") is True
    assert await limiter.check_and_consume("ip1") is False
    # Jump 3 seconds — 3 tokens refilled, capped at 2.
    monkeypatch.setattr(
        "app.security.rate_limit.time.monotonic", lambda: base + 3
    )
    assert await limiter.check_and_consume("ip1") is True
    assert await limiter.check_and_consume("ip1") is True
    assert await limiter.check_and_consume("ip1") is False


def test_constructor_rejects_invalid_args():
    with pytest.raises(ValueError):
        RateLimiter(capacity=0, refill_per_second=1.0)
    with pytest.raises(ValueError):
        RateLimiter(capacity=-1, refill_per_second=1.0)
    with pytest.raises(ValueError):
        RateLimiter(capacity=10, refill_per_second=0.0)
    with pytest.raises(ValueError):
        RateLimiter(capacity=10, refill_per_second=-0.1)


@pytest.mark.asyncio
async def test_reset_clears_buckets():
    limiter = RateLimiter(capacity=1, refill_per_second=0.001)
    await limiter.check_and_consume("ip1")
    assert await limiter.check_and_consume("ip1") is False
    limiter.reset()
    assert await limiter.check_and_consume("ip1") is True
