"""Tests for the bot-report instrument-exclusion helpers."""
from __future__ import annotations

from app.models.broker_orders import BrokerOrder
from app.services.broker_orders import is_excluded, parse_exclusions


def _order(isin="IRO1PNES0001", symbol="شپنا", symbol_title=None):
    return BrokerOrder(isin=isin, symbol=symbol, symbol_title=symbol_title)


def test_parse_exclusions_splits_lines_commas_semicolons():
    raw = "IRB123\n IRO1PNES0001 ,شپنا;\n\n IRTEST"
    assert parse_exclusions(raw) == {"IRB123", "IRO1PNES0001", "شپنا", "IRTEST"}


def test_parse_exclusions_uppercases_and_trims():
    assert parse_exclusions("  iro1pnes0001  ") == {"IRO1PNES0001"}


def test_parse_exclusions_empty():
    assert parse_exclusions("") == set()
    assert parse_exclusions(None) == set()
    assert parse_exclusions("   \n , ; ") == set()


def test_is_excluded_matches_isin_case_insensitive():
    assert is_excluded(_order(isin="IRO1PNES0001"), {"IRO1PNES0001"}) is True
    assert is_excluded(_order(isin="iro1pnes0001"), {"IRO1PNES0001"}) is True


def test_is_excluded_matches_symbol():
    assert is_excluded(_order(symbol="شپنا"), {"شپنا"}) is True


def test_is_excluded_matches_symbol_title():
    assert is_excluded(_order(symbol=None, symbol_title="عیار"), {"عیار"}) is True


def test_is_excluded_no_match():
    assert is_excluded(_order(isin="IRO1PNES0001", symbol="شپنا"), {"IRBOND0001"}) is False


def test_is_excluded_empty_set_never_excludes():
    assert is_excluded(_order(), set()) is False
