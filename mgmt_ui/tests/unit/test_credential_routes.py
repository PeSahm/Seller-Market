"""Route tests for the review fixes on PR #167.

F3 — the verify-credentials routes must NOT call the broker when the username is
blank (a captcha solve + ~5 login attempts wasted on a guaranteed reject).
F4 — the admin customers list must whitelist the enum-backed `cred`/`status`
query params so a garbage value degrades to "no filter" instead of a 500
(Postgres enum-cast error).

Driven in-process with a TestClient (the app serves fine without a live DB — the
DB-touching background tasks fail in the background but don't block startup; the
handlers under test get their DB via an overridden dependency).
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import settings as settings_mod


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setenv("MGMT_CSRF_SECRET", "z" * 40)
    for k in (
        "ENABLE_WORKER_LEADER_ELECTION", "ENABLE_INSTANCE_HEARTBEAT",
        "ENABLE_DB_AUTO_FAILOVER", "ENABLE_SERVICE_PROBE_WORKER",
        "ENABLE_STACK_HEALTH_WORKER", "ENABLE_SCHEDULED_RUN_INGESTOR",
        "ENABLE_FIRE_LOG_INGESTOR",
    ):
        monkeypatch.setenv(k, "false")
    settings_mod.get_settings.cache_clear()
    from app.main import create_app
    from app.db import get_db
    from app.security.deps import get_current_user, require_admin

    app = create_app()

    async def _fake_db():
        yield SimpleNamespace()

    app.dependency_overrides[get_db] = _fake_db
    yield app, get_current_user, require_admin
    app.dependency_overrides.clear()
    settings_mod.get_settings.cache_clear()


def _user(role):
    return SimpleNamespace(id=uuid.uuid4(), username="tester", role=role)


def _csrf_post(client, url, data):
    client.get("/health")  # mints + sets the csrf_token cookie
    token = client.cookies.get("csrf_token")
    return client.post(url, data=data, headers={"X-CSRF-Token": token})


# --------------------------------------------------------------------------
# F3 — blank username short-circuits before the broker probe
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "route,dep_name,role",
    [
        ("/agent/customers/verify-credentials", "get_current_user", "agent"),
        ("/admin/customers/verify-credentials", "require_admin", "admin"),
    ],
)
def test_verify_blank_username_skips_broker(app_client, monkeypatch, route, dep_name, role):
    app, get_current_user, require_admin = app_client
    dep = {"get_current_user": get_current_user, "require_admin": require_admin}[dep_name]
    app.dependency_overrides[dep] = lambda: _user(role)

    from app.services import broker_client
    spy = AsyncMock(side_effect=AssertionError("verify_credentials must NOT be called"))
    monkeypatch.setattr(broker_client, "verify_credentials", spy)

    with TestClient(app) as client:
        r = _csrf_post(client, route, {
            "broker": "bbi", "username": "", "password": "pw",
            "broker_fallback": "", "username_fallback": "",
        })
    assert r.status_code == 200
    assert "username" in r.text.lower()
    spy.assert_not_awaited()


# --------------------------------------------------------------------------
# The verify button routes through the resilient helper (mgmt-direct +
# trading-host proxy fallback) so brokers unreachable from mgmt (ideal) verify.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "route,dep_name,role",
    [
        ("/agent/customers/verify-credentials", "get_current_user", "agent"),
        ("/admin/customers/verify-credentials", "require_admin", "admin"),
    ],
)
def test_verify_button_uses_resilient(app_client, monkeypatch, route, dep_name, role):
    app, get_current_user, require_admin = app_client
    dep = {"get_current_user": get_current_user, "require_admin": require_admin}[dep_name]
    app.dependency_overrides[dep] = lambda: _user(role)

    from app.services import broker_verify, broker_client
    from app.services.brokers.base import CredStatus, VerifyResult

    # The mgmt-direct path must NOT be called by the route directly — it goes
    # through the resilient orchestrator, which decides direct-vs-proxy.
    direct_spy = AsyncMock(side_effect=AssertionError("route must call resilient, not direct"))
    monkeypatch.setattr(broker_client, "verify_credentials", direct_spy)
    resilient = AsyncMock(return_value=VerifyResult(
        ok=True, status=CredStatus.VALID, full_name="Verified Person"))
    monkeypatch.setattr(broker_verify, "verify_credentials_resilient", resilient)

    import app.routers.admin as admin_mod
    import app.routers.agent as agent_mod
    for mod in (admin_mod, agent_mod):
        monkeypatch.setattr(mod.settings_store, "get_setting", AsyncMock(return_value="http://ocr"))

    with TestClient(app) as client:
        r = _csrf_post(client, route, {
            "broker": "ideal", "username": "1263381952", "password": "pw",
            "broker_fallback": "", "username_fallback": "",
        })
    assert r.status_code == 200
    assert "Verified Person" in r.text
    resilient.assert_awaited_once()
    direct_spy.assert_not_awaited()


# --------------------------------------------------------------------------
# F4 — admin list whitelists garbage cred/status to "no filter" (no 500)
# --------------------------------------------------------------------------
def test_admin_customers_whitelists_bad_filters(app_client, monkeypatch):
    app, _gcu, require_admin = app_client
    app.dependency_overrides[require_admin] = lambda: _user("admin")

    import app.routers.admin as admin_mod
    captured = {}

    async def _fake_list_customers(db, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(admin_mod.services_customers, "list_customers", _fake_list_customers)
    monkeypatch.setattr(admin_mod.services_customers, "get_customer_trade_counts", AsyncMock(return_value={}))
    monkeypatch.setattr(admin_mod.services_agents, "list_agents", AsyncMock(return_value=[]))
    monkeypatch.setattr(admin_mod.services_servers, "list_servers", AsyncMock(return_value=[]))
    monkeypatch.setattr(admin_mod.brokers_admin, "list_all_grouped", AsyncMock(return_value=[]))

    with TestClient(app) as client:
        r = client.get("/admin/customers?cred=garbage&status=garbage")
    assert r.status_code == 200
    # both enum filters degraded to None (no filter) rather than reaching the DB
    assert captured.get("credential_status") is None
    assert captured.get("status") is None


def test_admin_customers_keeps_valid_filters(app_client, monkeypatch):
    app, _gcu, require_admin = app_client
    app.dependency_overrides[require_admin] = lambda: _user("admin")

    import app.routers.admin as admin_mod
    captured = {}

    async def _fake_list_customers(db, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(admin_mod.services_customers, "list_customers", _fake_list_customers)
    monkeypatch.setattr(admin_mod.services_customers, "get_customer_trade_counts", AsyncMock(return_value={}))
    monkeypatch.setattr(admin_mod.services_agents, "list_agents", AsyncMock(return_value=[]))
    monkeypatch.setattr(admin_mod.services_servers, "list_servers", AsyncMock(return_value=[]))
    monkeypatch.setattr(admin_mod.brokers_admin, "list_all_grouped", AsyncMock(return_value=[]))

    with TestClient(app) as client:
        r = client.get("/admin/customers?cred=invalid&status=active")
    assert r.status_code == 200
    assert captured.get("credential_status") == "invalid"
    assert captured.get("status") == "active"
