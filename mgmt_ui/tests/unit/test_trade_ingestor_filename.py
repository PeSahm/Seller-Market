"""Unit tests for the filename regex.

The bot writes ``{username}_{broker_code}_{YYYYMMDD_HHMMSS}.json`` —
the regex pins that contract so a future bot refactor doesn't silently
break ingestion.
"""
from __future__ import annotations

import pytest


def _get_re():
    """Late import so the test still runs even if the service module
    isn't fully loaded yet during parallel-agent integration."""
    from app.services.trade_ingestor import _FILENAME_RE
    return _FILENAME_RE


@pytest.mark.parametrize("name", [
    "4580090306_bbi_20260517_083001.json",
    "0780203674_ib_20260517_000000.json",
    "ab12_gs_20260517_235959.json",
    "user-with-dash_karamad_20260517_120000.json",
    "user_with_underscore_karamad_20260517_120000.json",  # username can contain underscores
])
def test_regex_accepts_canonical_filenames(name):
    rx = _get_re()
    m = rx.match(name)
    assert m is not None, f"expected match: {name!r}"


@pytest.mark.parametrize("name", [
    "no_broker_or_ts.json",
    "user_BBI_20260517_083001.json",         # broker uppercase rejected
    "user_bbi_20260517.json",                # missing time
    "user_bbi_20260517_0830.json",           # short time
    "user_bbi_20260517-083001.json",         # wrong separator
    "user_bbi_20260517_083001.JSON",         # uppercase ext
    "user_bbi_20260517_083001",              # no .json
    ".gitignore",                            # nope
    "user_bbi_20260517_083001.json.bak",     # has trailing .bak
])
def test_regex_rejects_malformed_filenames(name):
    rx = _get_re()
    assert rx.match(name) is None, f"expected no match: {name!r}"


def test_regex_captures_groups():
    rx = _get_re()
    m = rx.match("4580090306_bbi_20260517_083001.json")
    assert m is not None
    assert m.group("username") == "4580090306"
    assert m.group("broker") == "bbi"
    assert m.group("ts") == "20260517_083001"
