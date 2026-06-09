"""Hermetic tests for the auto-sell ladder engine (#110).

No network, no broker — ``fetch_holdings`` / ``place_order`` / ``sleep`` are
fakes. Run: ``python -m pytest test_auto_sell_engine.py -q``.
"""
from __future__ import annotations

from auto_sell_engine import chunk_volumes, sell_entire_position


# ---------------------------------------------------------------------------
# chunk_volumes
# ---------------------------------------------------------------------------

def test_chunk_volumes_operator_example():
    # The operator's exact example: 1001 shares, max 100 -> 10x100 + 1.
    assert chunk_volumes(1001, 100) == [100] * 10 + [1]
    assert sum(chunk_volumes(1001, 100)) == 1001


def test_chunk_volumes_exact_multiple():
    assert chunk_volumes(300, 100) == [100, 100, 100]


def test_chunk_volumes_under_cap_is_single_order():
    assert chunk_volumes(40, 100) == [40]
    assert chunk_volumes(100, 100) == [100]


def test_chunk_volumes_no_cap():
    # max<=0 (unknown mxqo) => one order for the whole holding.
    assert chunk_volumes(1001, 0) == [1001]
    assert chunk_volumes(1001, None) == [1001]


def test_chunk_volumes_empty():
    assert chunk_volumes(0, 100) == []
    assert chunk_volumes(-5, 100) == []


# ---------------------------------------------------------------------------
# sell_entire_position
# ---------------------------------------------------------------------------

def _fakes(holdings_sequence, status=200):
    """Return (fetch_holdings, place_order, calls, fires) with scripted holdings."""
    seq = list(holdings_sequence)
    calls = []
    fires = []

    def fetch_holdings():
        return seq.pop(0) if seq else 0

    def place_order(price, volume):
        calls.append((price, volume))
        return status, b"ok"

    def emit_fire(volume, body):
        fires.append((volume, body))

    return fetch_holdings, place_order, calls, fires, emit_fire


def test_sell_fires_full_ladder_at_floor_and_goes_flat():
    # holdings 1001 before, 0 after -> 11 chunks all at floor=5, flat, one fire.
    fetch, place, calls, fires, emit = _fakes([1001, 0])
    res = sell_entire_position(
        isin="IRO1X", floor_price=5, max_order_volume=100,
        fetch_holdings=fetch, place_order=place, emit_fire=emit,
        sleep=lambda _s: None,
    )
    assert [v for _p, v in calls] == [100] * 10 + [1]
    assert all(p == 5 for p, _v in calls)        # every chunk at the floor
    assert res.chunks_fired == 11
    assert res.flat is True
    assert len(fires) == 1                        # fire-log emitted once


def test_sell_aborts_on_bad_floor():
    fetch, place, calls, fires, emit = _fakes([1001, 0])
    res = sell_entire_position(
        isin="IRO1X", floor_price=0, max_order_volume=100,
        fetch_holdings=fetch, place_order=place, emit_fire=emit, sleep=lambda _s: None,
    )
    assert calls == []                            # nothing fired
    assert res.error == "bad floor price"
    assert res.flat is False


def test_sell_noop_when_nothing_held():
    fetch, place, calls, fires, emit = _fakes([0])
    res = sell_entire_position(
        isin="IRO1X", floor_price=5, max_order_volume=100,
        fetch_holdings=fetch, place_order=place, emit_fire=emit, sleep=lambda _s: None,
    )
    assert calls == []
    assert res.flat is True
    assert res.chunks_fired == 0


def test_sell_not_flat_when_holdings_remain():
    # Broker filled nothing (holdings unchanged) -> not flat; monitor will re-fire.
    fetch, place, calls, fires, emit = _fakes([1001, 1001])
    res = sell_entire_position(
        isin="IRO1X", floor_price=5, max_order_volume=100,
        fetch_holdings=fetch, place_order=place, emit_fire=emit, sleep=lambda _s: None,
    )
    assert res.flat is False
    assert res.holdings_after == 1001


def test_sell_rejected_chunks_count_as_not_fired_no_fire():
    # Every chunk rejected (HTTP 500) -> chunks_fired 0, no fire-log line.
    fetch, place, calls, fires, emit = _fakes([100, 100], status=500)
    res = sell_entire_position(
        isin="IRO1X", floor_price=5, max_order_volume=100,
        fetch_holdings=fetch, place_order=place, emit_fire=emit, sleep=lambda _s: None,
    )
    assert len(calls) == 1            # 100 holdings, cap 100 -> single chunk
    assert res.chunks_fired == 0
    assert fires == []


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
