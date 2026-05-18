"""Unit tests for ``app.security.csrf`` low-level token primitives.

These tests exercise ``_build_token`` and ``_verify_token`` directly so
the cryptographic guarantees are pinned down without the cost of an
ASGI integration harness. End-to-end coverage (cookie set on GET,
header/form acceptance on POST, 403 on mismatch) is provided by the
sibling agents' integration suites.
"""

from __future__ import annotations

import time

import app.security.csrf as csrf_mod
from app.security.csrf import (
    TOKEN_TTL_SECONDS,
    _build_token,
    _verify_token,
)


# A secret long enough that the production min_length=32 guard would
# accept it; the helpers themselves don't enforce that, but using a
# realistic value catches any accidental ``str`` vs ``bytes`` typo.
SECRET = b"unit-test-csrf-secret-32-bytes-or-more-xxxx"


def test_round_trip_with_same_secret_verifies() -> None:
    """A freshly built token must verify under the same secret."""
    token = _build_token(SECRET)
    assert _verify_token(SECRET, token) is True


def test_tampered_signature_fails() -> None:
    """Flipping a byte in the HMAC suffix must break verification."""
    token = _build_token(SECRET)
    # The token is hex(nonce[16] || ts[8] || hmac[32]) = 56 raw bytes
    # = 112 hex chars. Mutating the LAST hex pair lands inside the HMAC
    # region, so signature verification must fail.
    last = token[-1]
    replacement = "0" if last != "0" else "1"
    tampered = token[:-1] + replacement
    assert _verify_token(SECRET, tampered) is False


def test_wrong_secret_fails() -> None:
    """A token built under one secret must not verify under another."""
    token = _build_token(SECRET)
    other = b"different-secret-of-the-required-length-xxxxx"
    assert _verify_token(other, token) is False


def test_non_hex_input_fails() -> None:
    """Garbage that isn't valid hex must be rejected without crashing."""
    assert _verify_token(SECRET, "not-a-hex-string!") is False


def test_too_short_token_fails() -> None:
    """A valid-hex but undersized token must be rejected by the length guard."""
    # 10 bytes -> 20 hex chars, well short of the required 56-byte / 112-hex
    # nonce + timestamp + hmac layout.
    short = "aa" * 10
    assert _verify_token(SECRET, short) is False


def test_expired_token_fails(monkeypatch) -> None:
    """A token older than ``TOKEN_TTL_SECONDS`` must be rejected."""
    # Build a token at ``t0``, then advance the clock past TTL and re-verify.
    t0 = 1_700_000_000
    monkeypatch.setattr(csrf_mod.time, "time", lambda: t0)
    token = _build_token(SECRET)
    # Sanity check: at issue time it verifies.
    assert _verify_token(SECRET, token) is True
    # Advance by TTL + 1 second to push the embedded timestamp out of bounds.
    monkeypatch.setattr(
        csrf_mod.time, "time", lambda: t0 + TOKEN_TTL_SECONDS + 1
    )
    assert _verify_token(SECRET, token) is False
