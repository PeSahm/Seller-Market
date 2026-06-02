"""Hermetic tests for broker_enum endpoint derivation (data-driven ephoenix).

Locks two guarantees:
* the enumerated brokers produce byte-for-byte the same endpoints as before
  (``BrokerCode.get_endpoints`` delegates to ``get_endpoints_for``), and
* a NEW, non-enumerated ephoenix code derives valid endpoints with no enum
  entry — so a broker added purely via the mgmt UI's ``brokers`` table can be
  traded by the bot with no image rebuild.
"""
from __future__ import annotations

from broker_enum import BrokerCode, get_endpoints_for


def test_enum_delegates_to_get_endpoints_for():
    # Every enumerated broker's endpoints == the code-string derivation, so the
    # refactor is byte-for-byte identical for the existing brokers.
    for b in BrokerCode:
        assert b.get_endpoints() == get_endpoints_for(b.value)


def test_standard_ephoenix_urls():
    ep = get_endpoints_for("ayandeh")
    assert ep["login"] == "https://identity-ayandeh.ephoenix.ir/api/v2/accounts/login"
    assert ep["order"] == "https://api-ayandeh.ephoenix.ir/api/v2/orders/NewOrder"
    assert ep["calculate_order"] == "https://api-ayandeh.ephoenix.ir/api/v2/orders/CalculateOrderParam"
    assert ep["open_orders"] == "https://api-ayandeh.ephoenix.ir/api/v2/orders/GetOpenOrders"
    assert ep["market_data"] == "https://mdapi1.ephoenix.ir/api/v2/instruments/full"
    assert ep["portfolio"] == (
        "https://backofficeexternal-ayandeh.ephoenix.ir"
        "/api/portfolio/getrealsecuritypositionbydate"
    )


def test_ib_is_special_cased():
    ep = get_endpoints_for("ib")
    assert ep["login"] == "https://identity.ibtrader.ir/api/v2/accounts/login"
    assert ep["order"] == "https://api.ibtrader.ir/api/v2/orders/NewOrder"
    assert ep["market_data"] == "https://mdapi.ibtrader.ir/api/v2/instruments/full"
    # portfolio + customer_info live on the api8 shard, not the regular api host.
    assert ep["portfolio"] == "https://api8.ibtrader.ir/api/portfolio/getrealsecuritypositionbydate"
    assert ep["customer_info"] == "https://api8.ibtrader.ir/api/party/getcustomerinfo"


def test_new_ephoenix_broker_needs_no_enum_entry():
    # A code that is NOT in the BrokerCode enum still derives valid endpoints,
    # so it can be added purely via the mgmt UI (a DB row) and traded by the bot.
    code = "newbank"
    assert code not in [b.value for b in BrokerCode]
    ep = get_endpoints_for(code)
    assert ep["order"] == "https://api-newbank.ephoenix.ir/api/v2/orders/NewOrder"
    assert ep["login"] == "https://identity-newbank.ephoenix.ir/api/v2/accounts/login"
    assert ep["market_data"] == "https://mdapi1.ephoenix.ir/api/v2/instruments/full"
