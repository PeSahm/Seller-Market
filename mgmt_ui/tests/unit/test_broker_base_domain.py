"""Unit tests for the per-broker OnlinePlus ``base_domain``.

OnlinePlus tenants don't share one host convention (Hafez = hafezbroker.ir,
dnovin = dnovinbr.ir), so each OnlinePlus broker carries a ``base_domain`` the
adapter builds ``online.{domain}`` / ``api.{domain}`` from. These tests pin the
schema validation, the registry cache, the adapter host derivation, and the
config.ini rendering. All pure/sync — no DB, no network.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

import app.services.brokers.onlineplus as op_mod
from app.schemas.broker import BrokerCreate, BrokerUpdate
from app.services.brokers import registry
from app.services.brokers.onlineplus import OnlinePlusAdapter


# --------------------------------------------------------------------------
# schema validation
# --------------------------------------------------------------------------
def test_base_domain_accepts_bare_domain():
    assert BrokerCreate(
        code="dnovin", family="onlineplus", label="X", base_domain="DNovinbr.IR"
    ).base_domain == "dnovinbr.ir"  # stripped + lowercased


def test_base_domain_empty_is_none():
    assert BrokerCreate(
        code="hafez", family="onlineplus", label="X", base_domain="  "
    ).base_domain is None
    assert BrokerCreate(code="gs", family="ephoenix", label="X").base_domain is None


@pytest.mark.parametrize(
    "bad",
    [
        "https://online.dnovinbr.ir",  # full URL (scheme)
        "online.dnovinbr.ir/Account",  # has a path
        "dnovin br.ir",                # space
        "dnovinbr",                    # no dot / TLD
        "http://dnovinbr.ir",
    ],
)
def test_base_domain_rejects_non_domain(bad):
    with pytest.raises(ValidationError):  # wraps the validator's ValueError
        BrokerCreate(code="x", family="onlineplus", label="X", base_domain=bad)


def test_base_domain_update_can_clear():
    # Update with empty -> None so the operator can clear it back to convention.
    cleared = BrokerUpdate(family="onlineplus", label="X", base_domain="")
    assert cleared.base_domain is None
    kept = BrokerUpdate(family="onlineplus", label="X", base_domain="dnovinbr.ir")
    assert kept.base_domain == "dnovinbr.ir"


# --------------------------------------------------------------------------
# registry cache
# --------------------------------------------------------------------------
@pytest.fixture
def caches():
    """Snapshot + restore the registry caches so we don't leak into other tests."""
    fam = registry._FAMILY_CACHE
    bd = registry._BASE_DOMAIN_CACHE
    registry.set_family_map({"hafez": "onlineplus", "dnovin": "onlineplus", "gs": "ephoenix"})
    registry.set_base_domain_map({"dnovin": "dnovinbr.ir"})
    try:
        yield
    finally:
        registry._FAMILY_CACHE = fam
        registry._BASE_DOMAIN_CACHE = bd


def test_base_domain_of(caches):
    assert registry.base_domain_of("dnovin") == "dnovinbr.ir"
    assert registry.base_domain_of("hafez") is None      # onlineplus, convention
    assert registry.base_domain_of("gs") is None          # ephoenix
    assert registry.base_domain_of("nope") is None        # unknown — never raises


# --------------------------------------------------------------------------
# adapter host derivation
# --------------------------------------------------------------------------
def test_adapter_uses_base_domain(caches):
    a = OnlinePlusAdapter("dnovin")
    assert a._web_base == "https://online.dnovinbr.ir"
    assert a._api_convention == "https://api.dnovinbr.ir"


def test_adapter_falls_back_to_code_convention(caches):
    # hafez has no base_domain -> the {code}broker.ir convention.
    a = OnlinePlusAdapter("hafez")
    assert a._web_base == "https://online.hafezbroker.ir"
    assert a._api_convention == "https://api.hafezbroker.ir"


async def test_adapter_api_resolve_fallback_uses_convention(caches, monkeypatch):
    """When the login-page scrape fails, _resolve_api_base returns the
    base_domain-derived api host, not a {code}broker.ir guess."""
    monkeypatch.setattr(op_mod, "_API_BASE_CACHE", {}, raising=True)

    class _BoomClient:
        async def get(self, *a, **k):
            raise op_mod.httpx.HTTPError("scrape failed")

    api = await OnlinePlusAdapter("dnovin")._resolve_api_base(_BoomClient())
    assert api == "https://api.dnovinbr.ir"


# --------------------------------------------------------------------------
# config.ini rendering
# --------------------------------------------------------------------------
def test_config_ini_renders_base_domain(caches):
    from uuid import UUID

    from app.services.rendering import CustomerRow, StackRenderContext, render_config_ini

    ctx = StackRenderContext(
        agent_id=UUID("12345678-1234-5678-1234-567812345678"),
        server_base_dir="/root/seller-market/agents",
        agent_image_tag="img:latest",
        ocr_service_url="http://ocr",
        customers=(
            CustomerRow("sec_dnovin", "u1", "p1", "dnovin", "IRO1A", 1),
            CustomerRow("sec_hafez", "u2", "p2", "hafez", "IRO1B", 1),
            CustomerRow("sec_gs", "u3", "p3", "gs", "IRO1C", 1),
        ),
    )
    out = render_config_ini(ctx)
    # dnovin (onlineplus + base_domain) emits the key.
    assert "onlineplus_base_domain = dnovinbr.ir" in out
    # hafez (onlineplus, no base_domain) and gs (ephoenix) do NOT.
    assert out.count("onlineplus_base_domain") == 1
    # families still rendered correctly.
    assert "broker_family = onlineplus" in out
    assert "broker_family = ephoenix" in out


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
