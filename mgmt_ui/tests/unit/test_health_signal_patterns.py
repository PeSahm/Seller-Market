"""Pattern catalogue tests for the health-signal scanner.

Pins the regex contract so a future catalogue edit can't silently break
an existing detection. We test each catalogue entry against at least one
realistic log line sample, plus the edge cases (empty, mixed case,
no-match, Persian-language insufficient-funds variant).

Late imports throughout so the catalogue can be inspected without the
SSH stack — a sanity check for the "no SSH at import time" acceptance
criterion.
"""
from __future__ import annotations

import pytest


def _get_patterns():
    from app.services.health_signals import _PATTERNS
    return _PATTERNS


def _get_match_line():
    from app.services.health_signals import match_line
    return match_line


def test_patterns_catalogue_non_empty():
    """Catalogue must have at least the 7 shipping patterns."""
    pats = _get_patterns()
    assert len(pats) >= 7


def test_patterns_have_required_attrs():
    """Each catalogue entry exposes regex / kind / severity / message."""
    pats = _get_patterns()
    for p in pats:
        assert hasattr(p, "regex")
        assert hasattr(p, "kind")
        assert hasattr(p, "severity")
        assert hasattr(p, "message")
        assert p.severity in ("info", "warning", "error", "critical")


def test_patterns_kinds_unique():
    """Two catalogue entries should not collide on ``kind``."""
    pats = _get_patterns()
    kinds = [p.kind for p in pats]
    assert len(kinds) == len(set(kinds)), f"duplicate kinds: {kinds}"


# --- Per-pattern positive samples ----------------------------------------


@pytest.mark.parametrize(
    "line,expected_kind,expected_severity",
    [
        # broker_rate_limit
        ("HTTP 429 from broker bbi", "broker_rate_limit", "warning"),
        ("response: too many requests, slow down", "broker_rate_limit", "warning"),
        # captcha_fail
        ("captcha failed after 3 retries", "captcha_fail", "warning"),
        ("WRONG CAPTCHA, login aborted", "captcha_fail", "warning"),
        ("invalid captcha solution", "captcha_fail", "warning"),
        ("captcha was incorrect", "captcha_fail", "warning"),
        # auth_failed
        ("broker returned 401 Unauthorized", "auth_failed", "error"),
        ("authentication failed for user x", "auth_failed", "error"),
        ("login failed: bad password", "auth_failed", "error"),
        ("unauthorised access", "auth_failed", "error"),  # British spelling
        # insufficient_funds
        (
            "insufficient buying power for order #42",
            "insufficient_funds",
            "info",
        ),
        ("insufficient funds on account", "insufficient_funds", "info"),
        ("insufficient balance", "insufficient_funds", "info"),
        # ocr_down
        (
            "easyocr unreachable: connection refused",
            "ocr_down",
            "critical",
        ),
        ("OCR unavailable, can't decode captcha", "ocr_down", "critical"),
        # broker_unreachable
        (
            "connection refused to broker bbi",
            "broker_unreachable",
            "error",
        ),
        (
            "connection reset by ephoenix peer",
            "broker_unreachable",
            "error",
        ),
        (
            "ConnectionError contacting ibtrader",
            "broker_unreachable",
            "error",
        ),
        # broker_timeout
        ("MaxRetries reached talking to broker", "broker_timeout", "error"),
        ("broker timed out after 30s", "broker_timeout", "error"),
        ("broker timeout", "broker_timeout", "error"),
    ],
)
def test_match_line_positives(line, expected_kind, expected_severity):
    match_line = _get_match_line()
    pat = match_line(line)
    assert pat is not None, f"expected a match for: {line!r}"
    assert pat.kind == expected_kind
    assert pat.severity == expected_severity


# --- Edge cases ----------------------------------------------------------


def test_match_line_empty_returns_none():
    match_line = _get_match_line()
    assert match_line("") is None


def test_match_line_whitespace_only_returns_none():
    match_line = _get_match_line()
    assert match_line("   \t\n") is None


def test_match_line_non_matching_line_returns_none():
    match_line = _get_match_line()
    assert match_line("INFO heartbeat ok") is None


def test_match_line_case_insensitive_mixed_case():
    """Mixed-case 401 / Unauthorized still matches auth_failed."""
    match_line = _get_match_line()
    pat = match_line("Broker Returned 401 UnAuThOrIzEd")
    assert pat is not None
    assert pat.kind == "auth_failed"


def test_match_line_persian_insufficient_funds():
    """The Persian 'موجودی کافی' alternation matches insufficient_funds."""
    match_line = _get_match_line()
    pat = match_line("broker error: موجودی کافی نیست")
    assert pat is not None
    assert pat.kind == "insufficient_funds"
    assert pat.severity == "info"


def test_match_line_persian_no_funds_variant():
    """'عدم موجودی' also matches insufficient_funds."""
    match_line = _get_match_line()
    pat = match_line("response from broker: عدم موجودی")
    assert pat is not None
    assert pat.kind == "insufficient_funds"


def test_module_import_does_not_require_ssh():
    """``_PATTERNS`` must be importable without paramiko / SSH config.

    This pins the "no SSH calls at module import" acceptance criterion —
    the lazy import of run_command lives inside scan_stack_once, not at
    module top-level.

    Earlier tests in the suite already import ``app.services.health_signals``,
    so a plain ``import`` here would be a cache hit on ``sys.modules`` and
    silently miss a regression where someone moves an SSH import back to
    module top-level. Force a fresh execution by popping the cached module
    and re-importing it.
    """
    import importlib
    import sys

    sys.modules.pop("app.services.health_signals", None)
    mod = importlib.import_module("app.services.health_signals")
    assert len(mod._PATTERNS) >= 7
