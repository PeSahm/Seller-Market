"""Tests for the Decimal / datetime coercion helpers."""
from __future__ import annotations

from datetime import timezone
from decimal import Decimal


def _get_helpers():
    from app.services.trade_ingestor import _decimal_from, _parse_created
    return _decimal_from, _parse_created


def test_decimal_from_int_returns_decimal():
    d, _ = _get_helpers()
    assert d(1000) == Decimal("1000")


def test_decimal_from_float_returns_decimal():
    d, _ = _get_helpers()
    assert d(1.5) == Decimal("1.5")


def test_decimal_from_str_returns_decimal():
    d, _ = _get_helpers()
    assert d("1234.5") == Decimal("1234.5")


def test_decimal_from_none_returns_none():
    d, _ = _get_helpers()
    assert d(None) is None


def test_decimal_from_garbage_returns_none():
    d, _ = _get_helpers()
    assert d("not a number") is None


def test_decimal_from_zero_is_zero():
    d, _ = _get_helpers()
    assert d(0) == Decimal("0")


def test_parse_created_iso_naive_assumes_utc():
    _, p = _get_helpers()
    dt = p("2026-05-17T08:30:01")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 5 and dt.day == 17
    assert dt.hour == 8 and dt.minute == 30 and dt.second == 1
    assert dt.tzinfo == timezone.utc


def test_parse_created_iso_with_microseconds():
    _, p = _get_helpers()
    dt = p("2026-05-17T08:30:01.123456")
    assert dt is not None
    assert dt.microsecond == 123456


def test_parse_created_none_returns_none():
    _, p = _get_helpers()
    assert p(None) is None


def test_parse_created_empty_returns_none():
    _, p = _get_helpers()
    assert p("") is None


def test_parse_created_garbage_returns_none():
    _, p = _get_helpers()
    assert p("not a date") is None
