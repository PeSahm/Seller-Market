"""Tests for locust_scaling.per_section_user_count (fixed_count guard)."""
from __future__ import annotations

from locust_scaling import per_section_user_count


def test_even_share_matches_multiplier():
    assert per_section_user_count(42, 14) == 3    # auto-scale 3× → 3/section
    assert per_section_user_count(100, 14) == 7   # manual floor of 100 → 7/section
    assert per_section_user_count(60, 20) == 3


def test_floor_at_one():
    assert per_section_user_count(10, 14) == 1    # users < sections → 1 each
    assert per_section_user_count(0, 5) == 1


def test_degenerate_and_bad_input():
    assert per_section_user_count(42, 0) == 1
    assert per_section_user_count(42, -3) == 1
    assert per_section_user_count("x", 14) == 1
    assert per_section_user_count(42, None) == 1
