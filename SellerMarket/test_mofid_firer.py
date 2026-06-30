"""Hermetic tests for the Mofid bounded firer (no network, injected clock/send).

Pins the money-path bounding: STOP at the first confirmed success, a HARD attempt
cap (the 1500/hr backstop), no POST before the window opens, and the server→local
window math. Run: ``python -m pytest test_mofid_firer.py -q``.
"""
from __future__ import annotations

from datetime import datetime

import mofid_firer
from broker_adapters import PreparedOrder


def _po():
    return PreparedOrder(
        order_url="https://api/batch", body="{}", bearer_token="JWT",
        signer=None, cookies=None, price=100.0, volume=1,
        extra_headers={"Referer": "r"},
    )


class _Clock:
    def __init__(self, start_ms):
        self.t = start_ms

    def now(self):
        return self.t

    def advance(self, ms):
        self.t += ms


def _run(send, *, start=1000, end=5000, now0=2000, max_attempts=40, interval_ms=10):
    """Fire with a fake clock that advances by interval each sleep + each send."""
    clk = _Clock(now0)

    def fake_send(po):
        clk.advance(1)  # a send takes ~no time
        return send()

    def fake_sleep(s):
        clk.advance(int(s * 1000))

    return mofid_firer.fire_batch_in_window(
        _po(), window_start_ms=start, window_end_ms=end,
        max_attempts=max_attempts, interval_ms=interval_ms,
        send=fake_send, ok=mofid_firer.mofid_response_ok,
        now_ms=clk.now, sleep=fake_sleep,
    )


def test_stops_on_first_success():
    calls = {"n": 0}

    def send():
        calls["n"] += 1
        # fail twice, then succeed
        if calls["n"] < 3:
            return (200, b'{"isSuccessful":false}')
        return (200, b'{"isSuccessful":true}')

    res = _run(send)
    assert res.fired is True
    assert res.attempts == 3
    assert calls["n"] == 3  # stopped immediately on success


def test_hard_attempt_cap():
    def send():
        return (200, b'{"isSuccessful":false}')  # never confirms

    res = _run(send, max_attempts=5, interval_ms=10, end=10_000_000)
    assert res.fired is False
    assert res.attempts == 5  # the hard cap, not unbounded


def test_no_fire_before_window():
    sent = {"n": 0}
    clk = _Clock(0)  # start BEFORE the window opens (window_start_ms=1000)

    def fake_send(po):
        sent["n"] += 1
        return (200, b'{"isSuccessful":true}')

    def fake_sleep(s):
        clk.advance(int(s * 1000) or 50)  # the pre-window sleeps are 0.05s

    res = mofid_firer.fire_batch_in_window(
        _po(), window_start_ms=1000, window_end_ms=5000,
        max_attempts=40, interval_ms=10,
        send=fake_send, now_ms=clk.now, sleep=fake_sleep,
    )
    assert res.fired is True
    assert sent["n"] == 1  # fired exactly once, only after the window opened
    assert clk.now() >= 1000


def test_transport_error_counts_then_continues():
    calls = {"n": 0}

    def fake_send(po):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection reset")
        return (200, b'{"isSuccessful":true}')

    clk = _Clock(2000)

    def fake_sleep(s):
        clk.advance(int(s * 1000) + 1)

    res = mofid_firer.fire_batch_in_window(
        _po(), window_start_ms=1000, window_end_ms=9_000_000,
        max_attempts=40, interval_ms=10,
        send=fake_send, now_ms=clk.now, sleep=fake_sleep,
    )
    assert res.fired is True
    assert res.attempts == 2  # the transport error counted as an attempt


def test_window_math_subtracts_offset():
    # server clock leads local by 1500ms → local target = local_epoch(hms) - 1500
    fixed = datetime(2026, 6, 30, 8, 0, 0)
    start, end = mofid_firer.compute_local_window_ms(
        "08:44:58.450", "08:45:00.900", 1500, now_fn=lambda: fixed,
    )
    base = int(fixed.replace(hour=8, minute=44, second=58, microsecond=450000).timestamp() * 1000)
    assert start == base - 1500
    assert end - start == 2450  # 08:45:00.900 - 08:44:58.450


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
