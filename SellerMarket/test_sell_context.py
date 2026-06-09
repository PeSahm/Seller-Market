"""Hermetic tests for the adapters' auto-sell ``open_sell_context`` (#110).

No network, no broker. The ephoenix client is faked; exir's session/holdings +
RLC price are monkeypatched. Run: ``python -m pytest test_sell_context.py -q``.
"""
from __future__ import annotations

import json

import ephoenix_adapter
import exir_adapter
import rlc_price
from ephoenix_adapter import EphoenixAdapter
from exir_adapter import ExirAdapter


# ---------------------------------------------------------------------------
# ephoenix
# ---------------------------------------------------------------------------

class _FakeEphoenixClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def authenticate(self):
        return "TOK123"

    def get_instrument_info(self, isin):
        return {"min_price": 5, "max_price": 20, "max_volume": 100,
                "title": "X", "symbol": "X"}

    def get_holdings(self, isin, use_cache=True):
        # auto-sell must read LIVE (use_cache=False).
        assert use_cache is False
        return 1001


def test_ephoenix_open_sell_context(monkeypatch):
    monkeypatch.setattr(ephoenix_adapter, "EphoenixAPIClient", _FakeEphoenixClient)
    adapter = EphoenixAdapter("ayandeh", "u", "p", lambda _b: "00000")

    ctx = adapter.open_sell_context(isin="IRO1X", config_section={})
    assert ctx.floor_price == 5            # = min_price
    assert ctx.max_order_volume == 100     # = max_volume
    assert ctx.fetch_holdings() == 1001

    p = ctx.prepare_chunk(100)
    body = json.loads(p.body)
    assert body["side"] == 2 and body["price"] == 5 and body["volume"] == 100
    assert p.bearer_token == "TOK123"
    assert p.signer is None and p.cookies is None


# ---------------------------------------------------------------------------
# exir
# ---------------------------------------------------------------------------

def test_exir_open_sell_context(monkeypatch):
    monkeypatch.setattr(rlc_price, "get_price_band", lambda isin, *a, **k: (20, 5))
    monkeypatch.setattr(rlc_price, "get_max_order_qty", lambda isin, *a, **k: 100)

    adapter = ExirAdapter(
        broker_code="khobregan", username="u", password="p",
        captcha_decoder=lambda _b: "12345",
    )
    # Fake the authenticated session + holdings (no captcha / network).
    monkeypatch.setattr(
        adapter, "_session",
        lambda: {"broker_id": 116, "nt": "12abcdefghij", "cookies": {"c": "1"}},
    )
    monkeypatch.setattr(adapter, "_holdings", lambda isin, d: 1001)

    ctx = adapter.open_sell_context(isin="IRO1X", config_section={})
    assert ctx.floor_price == 5            # = lap (lower band)
    assert ctx.max_order_volume == 100     # = mxqo
    assert ctx.fetch_holdings() == 1001

    p = ctx.prepare_chunk(100)
    body = json.loads(p.body)
    assert body["side"] == "SIDE_SALE"
    assert body["price"] == "5.0"          # float-string format, matching prepare_order
    assert body["quantity"] == "100"
    assert body["brokerCode"] == 116
    assert body["insMaxLcode"] == "IRO1X"
    assert p.bearer_token is None
    assert p.signer is not None            # fresh X-App-N signer
    assert p.cookies == {"c": "1"}


def test_exir_open_sell_context_aborts_on_bad_floor(monkeypatch):
    monkeypatch.setattr(rlc_price, "get_price_band", lambda isin, *a, **k: (0, 0))
    monkeypatch.setattr(rlc_price, "get_max_order_qty", lambda isin, *a, **k: 100)
    adapter = ExirAdapter(broker_code="khobregan", username="u", password="p",
                          captcha_decoder=lambda _b: "12345")
    import pytest
    with pytest.raises(ValueError):
        adapter.open_sell_context(isin="IRO1X", config_section={})


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
