"""Hermetic tests for OrderResult.is_executed (trade-detection fix).

Regression guard: `locustfile_new.on_test_stop` calls `order.is_executed()` to
count executed trades; the method was missing, so every post-run summary raised
AttributeError and dropped the account from the count.
"""
from __future__ import annotations

from order_tracker import OrderResult


def test_is_executed_true_when_filled():
    o = OrderResult({"executedVolume": 5000, "volume": 5000, "state": 3})
    assert o.is_executed() is True


def test_is_executed_partial_fill_counts():
    o = OrderResult({"executedVolume": 1, "volume": 5000, "state": 2})
    assert o.is_executed() is True


def test_is_executed_false_when_queued():
    # Freshly-registered limit order at the open: state 2, nothing traded yet.
    o = OrderResult({"executedVolume": 0, "volume": 30000, "state": 2})
    assert o.is_executed() is False


def test_is_executed_handles_missing_or_bad():
    assert OrderResult({}).is_executed() is False           # executed_volume defaults 0
    assert OrderResult({"executedVolume": None}).is_executed() is False
    assert OrderResult({"executedVolume": "x"}).is_executed() is False
