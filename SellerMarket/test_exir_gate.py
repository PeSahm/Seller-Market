"""Unit tests for the Exir-only order-fire gate (``exir_gate``).

The pure decision logic is tested here without importing ``locustfile_new``
(which exits at import when there's no config.ini). ``on_start``'s glue —
ephoenix skip (`signer is None`) + `gevent.sleep(delay)` — is thin and verified
by code review + the real-open log check; the *decisions* live here.
"""
from datetime import datetime

import exir_gate as g


def test_parse_fire_at_formats():
    assert g.parse_fire_at("08:44:59") == datetime.strptime("08:44:59", "%H:%M:%S").time()
    assert g.parse_fire_at("08:44:59.000") == datetime.strptime("08:44:59.000", "%H:%M:%S.%f").time()
    assert g.parse_fire_at("08:44:59.250").microsecond == 250000


def test_parse_fire_at_invalid_is_none():
    for bad in ("garbage", "", None, "25:00:00", "8:44", "08:60:00"):
        assert g.parse_fire_at(bad) is None


def test_gate_holds_before_target():
    # run starts 08:30, fire-time 08:44:59 → hold ~899s.
    delay = g.gate_delay("08:44:59.000", datetime(2026, 6, 16, 8, 30, 0))
    assert delay is not None and abs(delay - 899.0) < 0.01


def test_gate_very_early_start_is_not_clamped():
    # A 5.75h-early start still HOLDS (no max-hold clamp — proves an early run
    # start never fires an early Exir send).
    delay = g.gate_delay("08:44:59.000", datetime(2026, 6, 16, 3, 0, 0))
    assert delay > 20000.0


def test_gate_past_target_fires_immediately():
    # The operator's "we are at 10:00 → gate useless, ok" case: negative delay
    # (caller fires now), using TODAY's date so it never rolls to tomorrow.
    delay = g.gate_delay("08:44:59.000", datetime(2026, 6, 16, 10, 0, 0))
    assert delay < 0


def test_gate_exactly_at_target_is_zero():
    # now == target → delay 0 → caller treats <= 0 as fire-now.
    delay = g.gate_delay("08:44:59.000", datetime(2026, 6, 16, 8, 44, 59, 0))
    assert abs(delay) < 1e-6


def test_gate_default_when_no_fire_at():
    # Exir section without a config `fire_at` → uses the module default.
    delay = g.gate_delay(None, datetime(2026, 6, 16, 8, 30, 0))
    assert abs(delay - 899.0) < 0.01


def test_gate_invalid_fire_at_disables():
    assert g.gate_delay("nonsense", datetime(2026, 6, 16, 8, 30, 0)) is None


def test_gate_sub_second_precision():
    delay = g.gate_delay("08:44:59.500", datetime(2026, 6, 16, 8, 44, 59, 0))
    assert abs(delay - 0.5) < 1e-6
