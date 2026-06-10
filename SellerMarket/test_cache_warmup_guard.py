"""cache_warmup: a non-positive BUY volume must be a QUIET SKIP.

Operator decision after the 2026-06-10 incident: an account with negative
buying power (-607,897 Rial) made the broker's CalculateOrderParam return a
negative volume (-30); the bot cached it and the run spammed 522 doomed order
POSTs, every one rejected with broker code 1001 "wrong order volume".

Warmup behavior now: log one warning, do NOT cache order params for that
section, and still count the account successful (the run-time prepare path
skips the section with its own one-line ValueError).
"""

import cache_warmup


class _FakeCache:
    def __init__(self):
        self.saved = []

    def save_order_params(self, **kw):
        self.saved.append(kw)


class _DebtAccountClient:
    """Negative BP → broker CalculateOrderParam returns a negative volume."""

    def __init__(self, **kw):
        pass

    def authenticate(self):
        return "TOKEN"

    def get_buying_power(self, use_cache=True):
        return -607_897.0

    def get_instrument_info(self, isin, use_cache=True):
        return {
            "title": "Sample", "symbol": "SMP",
            "max_price": 20_030, "min_price": 18_000,
            "max_volume": 100_000, "min_volume": 1,
        }

    def calculate_order_volume(self, isin, side, buying_power, price):
        return -30


class _HealthyClient(_DebtAccountClient):
    def get_buying_power(self, use_cache=True):
        return 5_000_000.0

    def calculate_order_volume(self, isin, side, buying_power, price):
        return 200


def _section():
    return {
        "username": "0073179957", "broker": "karamad", "password": "pw",
        "isin": "IRO1MSMI0001", "side": "1",
    }


def test_buy_volume_nonpositive_is_quiet_skip(monkeypatch):
    monkeypatch.setattr(cache_warmup, "EphoenixAPIClient", _DebtAccountClient)
    cache = _FakeCache()
    ok = cache_warmup.warmup_account(_section(), cache)
    assert ok is True          # account still counts successful (quiet skip)
    assert cache.saved == []   # order params NOT cached for the doomed section


def test_buy_volume_positive_still_caches(monkeypatch):
    monkeypatch.setattr(cache_warmup, "EphoenixAPIClient", _HealthyClient)
    cache = _FakeCache()
    ok = cache_warmup.warmup_account(_section(), cache)
    assert ok is True
    assert len(cache.saved) == 1
    assert cache.saved[0]["volume"] == 200
