"""Regression test for the length-prefixed hash_lock_key fix.

The hashing function feeds each argument into BLAKE2b with a 4-byte length
prefix so that ``("a|b",)`` and ``("a", "b")`` (which would otherwise have
identical UTF-8 byte streams) hash to different advisory-lock keys.
"""

from __future__ import annotations

from app.db import hash_lock_key


def test_no_collision_between_ambiguous_inputs() -> None:
    """('a|b',) and ('a', 'b') must hash to different keys."""
    assert hash_lock_key("a|b") != hash_lock_key("a", "b")


def test_stable_across_calls() -> None:
    """Same inputs in the same process must produce the same key."""
    assert hash_lock_key("stack", "abc") == hash_lock_key("stack", "abc")


def test_returns_signed_int() -> None:
    """Postgres advisory-lock keys are bigint (signed 64-bit)."""
    key = hash_lock_key("server", 1, "compose")
    assert isinstance(key, int)
    assert -(2 ** 63) <= key < 2 ** 63


def test_different_part_counts_diverge() -> None:
    """One arg vs three args should clearly diverge."""
    assert hash_lock_key("x") != hash_lock_key("x", "y", "z")


def test_handles_int_parts() -> None:
    """Integer parts are coerced to strings deterministically."""
    assert hash_lock_key("server", 1) == hash_lock_key("server", "1")


def test_empty_string_distinct_from_no_arg() -> None:
    """Hashing an empty string must differ from hashing two empty strings."""
    assert hash_lock_key("") != hash_lock_key("", "")
