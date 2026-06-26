"""Unit tests for resilient credential verification with a Tebyan-host proxy.

Some broker identity hosts (e.g. ``ideal`` → identity-ideal.ephoenix.ir,
185.115.151.x / AS214751) are unroutable from the mgmt hosts but reachable from
the Tebyan trading hosts. ``broker_verify`` runs the verify mgmt-direct when the
broker is reachable, and re-runs it THROUGH a trading host's bot container when
it isn't. Fully hermetic: the reachability probe, the broker verify, the SSH
``run_command`` and the server list are all stubbed.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import broker_verify
from app.services.brokers.base import CredStatus, VerifyResult


def _server(host="185.232.152.5", name="Tebyan-Mostafa-5"):
    return SimpleNamespace(id=uuid.uuid4(), host=host, name=name)


# ---------------------------------------------------------------------------
# _payload_to_result — pure mapping from the in-container JSON
# ---------------------------------------------------------------------------
def test_payload_valid_maps_to_ok_with_name():
    r = broker_verify._payload_to_result(
        {"status": "valid", "detail": "login ok", "full_name": "Ali",
         "national_id": "1263381952"}
    )
    assert r.ok is True
    assert r.status is CredStatus.VALID
    assert r.full_name == "Ali"
    assert r.national_id == "1263381952"


def test_payload_invalid_maps_to_invalid_credentials():
    r = broker_verify._payload_to_result(
        {"status": "invalid_credentials", "detail": "broker rejected credentials"}
    )
    assert r.ok is False
    assert r.status is CredStatus.INVALID_CREDENTIALS
    assert "rejected" in (r.error or "")


def test_payload_transient_and_none_map_to_transient():
    assert broker_verify._payload_to_result(
        {"status": "transient", "detail": "retries exhausted"}
    ).status is CredStatus.TRANSIENT
    none_r = broker_verify._payload_to_result(None)
    assert none_r.ok is False and none_r.status is CredStatus.TRANSIENT


# ---------------------------------------------------------------------------
# verify_via_trading_host — find a host with a bot container, run, parse
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_proxy_skips_hosts_without_bot_then_verifies(monkeypatch):
    s1, s2 = _server(host="185.232.152.177"), _server(host="185.232.152.5")
    monkeypatch.setattr(broker_verify, "list_servers", AsyncMock(return_value=[s1, s2]))

    async def _find(server, **kw):
        return None if server is s1 else "sm-agent-x-bot"

    monkeypatch.setattr(broker_verify, "_find_bot_container", _find)
    calls: list = []

    async def _run(server, cmd, **kw):
        calls.append(server.host)
        return SimpleNamespace(
            stdout=json.dumps({"status": "valid", "detail": "login ok",
                               "full_name": "Ali"}),
            stderr="",
        )

    monkeypatch.setattr(broker_verify, "run_command", _run)

    r = await broker_verify.verify_via_trading_host(
        db=None, broker_code="ideal", family="ephoenix",
        username="u", password="p", isin="IRO1SROD0001",
    )
    assert r.status is CredStatus.VALID and r.full_name == "Ali"
    assert calls == ["185.232.152.5"]  # only the host that HAD a bot container ran


@pytest.mark.asyncio
async def test_proxy_returns_invalid_immediately(monkeypatch):
    s1 = _server()
    monkeypatch.setattr(broker_verify, "list_servers", AsyncMock(return_value=[s1]))
    monkeypatch.setattr(broker_verify, "_find_bot_container", AsyncMock(return_value="bot"))
    monkeypatch.setattr(broker_verify, "run_command", AsyncMock(return_value=SimpleNamespace(
        stdout=json.dumps({"status": "invalid_credentials", "detail": "bad password"}),
        stderr="",
    )))
    r = await broker_verify.verify_via_trading_host(
        db=None, broker_code="ideal", family="ephoenix",
        username="u", password="p", isin="x",
    )
    assert r.status is CredStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_proxy_transient_host_falls_through_to_next(monkeypatch):
    s1, s2 = _server(host="a"), _server(host="b")
    monkeypatch.setattr(broker_verify, "list_servers", AsyncMock(return_value=[s1, s2]))
    monkeypatch.setattr(broker_verify, "_find_bot_container", AsyncMock(return_value="bot"))

    async def _run(server, cmd, **kw):
        if server is s1:
            return SimpleNamespace(stdout="garbage no json", stderr="")
        return SimpleNamespace(stdout=json.dumps({"status": "valid"}), stderr="")

    monkeypatch.setattr(broker_verify, "run_command", _run)
    r = await broker_verify.verify_via_trading_host(
        db=None, broker_code="ideal", family="ephoenix",
        username="u", password="p", isin="x",
    )
    assert r.status is CredStatus.VALID


@pytest.mark.asyncio
async def test_proxy_run_command_error_tries_next_host(monkeypatch):
    s1, s2 = _server(host="a"), _server(host="b")
    monkeypatch.setattr(broker_verify, "list_servers", AsyncMock(return_value=[s1, s2]))
    monkeypatch.setattr(broker_verify, "_find_bot_container", AsyncMock(return_value="bot"))

    async def _run(server, cmd, **kw):
        if server is s1:
            raise RuntimeError("Channel closed")
        return SimpleNamespace(stdout=json.dumps({"status": "valid"}), stderr="")

    monkeypatch.setattr(broker_verify, "run_command", _run)
    r = await broker_verify.verify_via_trading_host(
        db=None, broker_code="ideal", family="ephoenix",
        username="u", password="p", isin="x",
    )
    assert r.status is CredStatus.VALID


@pytest.mark.asyncio
async def test_proxy_no_host_with_bot_is_transient(monkeypatch):
    monkeypatch.setattr(broker_verify, "list_servers", AsyncMock(return_value=[_server()]))
    monkeypatch.setattr(broker_verify, "_find_bot_container", AsyncMock(return_value=None))
    r = await broker_verify.verify_via_trading_host(
        db=None, broker_code="ideal", family="ephoenix",
        username="u", password="p", isin="x",
    )
    assert r.ok is False and r.status is CredStatus.TRANSIENT


# ---------------------------------------------------------------------------
# verify_credentials_resilient — the orchestration
# ---------------------------------------------------------------------------
def _patch_broker_lookup(monkeypatch, family="ephoenix"):
    monkeypatch.setattr(
        broker_verify, "get_broker_by_code",
        AsyncMock(return_value=SimpleNamespace(family=family, label="Ideal")),
    )


@pytest.mark.asyncio
async def test_resilient_reachable_valid_skips_proxy(monkeypatch):
    _patch_broker_lookup(monkeypatch)
    monkeypatch.setattr(broker_verify, "_reachable_from_mgmt", AsyncMock(return_value=True))
    from app.services import broker_client
    monkeypatch.setattr(broker_client, "verify_credentials",
                        AsyncMock(return_value=VerifyResult(ok=True, status=CredStatus.VALID)))
    proxy = AsyncMock()
    monkeypatch.setattr(broker_verify, "verify_via_trading_host", proxy)

    r = await broker_verify.verify_credentials_resilient(
        db=None, broker_code="bbi", username="u", password="p", ocr_service_url="o",
    )
    assert r.status is CredStatus.VALID
    proxy.assert_not_called()


@pytest.mark.asyncio
async def test_resilient_reachable_invalid_skips_proxy(monkeypatch):
    _patch_broker_lookup(monkeypatch)
    monkeypatch.setattr(broker_verify, "_reachable_from_mgmt", AsyncMock(return_value=True))
    from app.services import broker_client
    monkeypatch.setattr(broker_client, "verify_credentials", AsyncMock(
        return_value=VerifyResult(ok=False, status=CredStatus.INVALID_CREDENTIALS)))
    proxy = AsyncMock()
    monkeypatch.setattr(broker_verify, "verify_via_trading_host", proxy)

    r = await broker_verify.verify_credentials_resilient(
        db=None, broker_code="bbi", username="u", password="p", ocr_service_url="o",
    )
    assert r.status is CredStatus.INVALID_CREDENTIALS
    proxy.assert_not_called()


@pytest.mark.asyncio
async def test_resilient_reachable_transient_falls_back_to_proxy(monkeypatch):
    _patch_broker_lookup(monkeypatch)
    monkeypatch.setattr(broker_verify, "_reachable_from_mgmt", AsyncMock(return_value=True))
    from app.services import broker_client
    monkeypatch.setattr(broker_client, "verify_credentials", AsyncMock(
        return_value=VerifyResult(ok=False, status=CredStatus.TRANSIENT)))
    monkeypatch.setattr(broker_verify, "verify_via_trading_host", AsyncMock(
        return_value=VerifyResult(ok=True, status=CredStatus.VALID)))

    r = await broker_verify.verify_credentials_resilient(
        db=None, broker_code="bbi", username="u", password="p", ocr_service_url="o",
    )
    assert r.status is CredStatus.VALID  # proxy's decisive verdict wins


@pytest.mark.asyncio
async def test_resilient_transient_both_keeps_original(monkeypatch):
    _patch_broker_lookup(monkeypatch)
    monkeypatch.setattr(broker_verify, "_reachable_from_mgmt", AsyncMock(return_value=True))
    from app.services import broker_client
    original = VerifyResult(ok=False, status=CredStatus.TRANSIENT, error="mgmt transient")
    monkeypatch.setattr(broker_client, "verify_credentials", AsyncMock(return_value=original))
    monkeypatch.setattr(broker_verify, "verify_via_trading_host", AsyncMock(
        return_value=VerifyResult(ok=False, status=CredStatus.TRANSIENT, error="proxy transient")))

    r = await broker_verify.verify_credentials_resilient(
        db=None, broker_code="bbi", username="u", password="p", ocr_service_url="o",
    )
    assert r is original  # both inconclusive → keep the mgmt-direct result


@pytest.mark.asyncio
async def test_resilient_unreachable_goes_straight_to_proxy(monkeypatch):
    _patch_broker_lookup(monkeypatch)
    monkeypatch.setattr(broker_verify, "_reachable_from_mgmt", AsyncMock(return_value=False))
    from app.services import broker_client
    direct = AsyncMock()
    monkeypatch.setattr(broker_client, "verify_credentials", direct)
    monkeypatch.setattr(broker_verify, "verify_via_trading_host", AsyncMock(
        return_value=VerifyResult(ok=True, status=CredStatus.VALID)))

    r = await broker_verify.verify_credentials_resilient(
        db=None, broker_code="ideal", username="u", password="p", ocr_service_url="o",
    )
    assert r.status is CredStatus.VALID
    direct.assert_not_called()  # mgmt-direct never attempted when unreachable
