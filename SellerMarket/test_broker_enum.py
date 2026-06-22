"""Hermetic tests for broker_enum endpoint derivation (data-driven ephoenix).

Locks two guarantees:
* the enumerated brokers produce byte-for-byte the same endpoints as before
  (``BrokerCode.get_endpoints`` delegates to ``get_endpoints_for``), and
* a NEW, non-enumerated ephoenix code derives valid endpoints with no enum
  entry — so a broker added purely via the mgmt UI's ``brokers`` table can be
  traded by the bot with no image rebuild.
"""
from __future__ import annotations

import pytest

import runtime_config
from broker_enum import BrokerCode, get_endpoints_for


@pytest.fixture(autouse=True)
def _clean_runtime():
    # No stale [runtime] snapshot leaking between tests; default path doesn't
    # exist so an un-injected test sees an empty section (== hardcoded fallback).
    runtime_config.reset_cache()
    yield
    runtime_config.reset_cache()


@pytest.fixture
def runtime(monkeypatch):
    """Inject a fake [runtime] section so endpoints read overrides, no file I/O."""
    data: dict[str, str] = {}
    monkeypatch.setattr(runtime_config, "_snapshot", lambda: data)
    return data


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
    assert ep["market_data"] == "https://marketdatagw.ephoenix.ir/api/v2/instruments/full"
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
    assert ep["market_data"] == "https://marketdatagw.ephoenix.ir/api/v2/instruments/full"


# ---------------------------------------------------------------------------
# [runtime] overrides — change endpoints fleet-wide with no image rebuild
# ---------------------------------------------------------------------------

def test_runtime_override_ephoenix_md_host(runtime):
    # Replays the S29 incident: the ephoenix market-data host moves, and a
    # single setting redirects every stack with no CI/image/redeploy.
    runtime["ephoenix_md_host"] = "newmdgw"
    ep = get_endpoints_for("ayandeh")
    assert ep["market_data"] == "https://newmdgw.ephoenix.ir/api/v2/instruments/full"
    # other endpoints unaffected
    assert ep["order"] == "https://api-ayandeh.ephoenix.ir/api/v2/orders/NewOrder"


def test_runtime_override_ephoenix_domain(runtime):
    runtime["ephoenix_domain"] = "ephoenix2.ir"
    ep = get_endpoints_for("ayandeh")
    assert ep["order"] == "https://api-ayandeh.ephoenix2.ir/api/v2/orders/NewOrder"
    assert ep["market_data"] == "https://marketdatagw.ephoenix2.ir/api/v2/instruments/full"


def test_runtime_override_ib(runtime):
    runtime["ib_domain"] = "ibtrader2.ir"
    runtime["ib_md_host"] = "mdapi2"
    runtime["ib_portfolio_shard"] = "api9"
    ep = get_endpoints_for("ib")
    assert ep["order"] == "https://api.ibtrader2.ir/api/v2/orders/NewOrder"
    assert ep["market_data"] == "https://mdapi2.ibtrader2.ir/api/v2/instruments/full"
    assert ep["portfolio"] == "https://api9.ibtrader2.ir/api/portfolio/getrealsecuritypositionbydate"


def test_endpoint_escape_hatch_full_url(runtime):
    # A single API path moved → override just that one endpoint, verbatim.
    runtime["endpoint_ib_order"] = "https://custom-host.example/api/v3/place"
    ep = get_endpoints_for("ib")
    assert ep["order"] == "https://custom-host.example/api/v3/place"
    # siblings untouched
    assert ep["login"] == "https://identity.ibtrader.ir/api/v2/accounts/login"
