"""Unit tests for the daily credential checker worker (_sweep_once).

Fully hermetic: AsyncSessionLocal, the broker verify, and the persistence call
are all stubbed. We assert the verdict mapping per customer AND that one bad
customer never aborts the sweep.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.brokers.base import CredStatus, VerifyResult
from app.workers import credential_checker


@asynccontextmanager
async def _fake_session_cm():
    yield SimpleNamespace()


def _patch_common(monkeypatch, customers, verify_results, recorder):
    """Wire up the module-level imports _sweep_once pulls in at call time."""
    import app.db as db_mod
    from app.services import broker_client, settings_store
    from app.services import customers as customers_svc

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: _fake_session_cm())
    monkeypatch.setattr(
        settings_store, "get_setting", AsyncMock(return_value="http://ocr")
    )
    monkeypatch.setattr(
        customers_svc, "list_customers", AsyncMock(return_value=customers)
    )
    monkeypatch.setattr(
        customers_svc, "decrypt_password", AsyncMock(return_value="pw")
    )

    async def _verify(*, broker_code, username, password, ocr_service_url):
        return verify_results[username]

    monkeypatch.setattr(broker_client, "verify_credentials", _verify)

    async def _set(db, cid, status, message=None, *, actor_id=None):
        recorder.append((cid, status))

    monkeypatch.setattr(customers_svc, "set_credential_status", _set)


@pytest.mark.asyncio
async def test_sweep_maps_each_verdict(monkeypatch):
    customers = [
        SimpleNamespace(id=uuid.uuid4(), broker="bbi", username="good"),
        SimpleNamespace(id=uuid.uuid4(), broker="bbi", username="bad"),
        SimpleNamespace(id=uuid.uuid4(), broker="bbi", username="flaky"),
    ]
    verify_results = {
        "good": VerifyResult(ok=True, status=CredStatus.VALID),
        "bad": VerifyResult(ok=False, status=CredStatus.INVALID_CREDENTIALS),
        "flaky": VerifyResult(ok=False, status=CredStatus.TRANSIENT),
    }
    recorder: list = []
    _patch_common(monkeypatch, customers, verify_results, recorder)

    checked, valid, invalid = await credential_checker._sweep_once()

    assert checked == 3 and valid == 1 and invalid == 1
    by_id = dict(recorder)
    assert by_id[customers[0].id] is CredStatus.VALID
    assert by_id[customers[1].id] is CredStatus.INVALID_CREDENTIALS
    assert by_id[customers[2].id] is CredStatus.TRANSIENT


@pytest.mark.asyncio
async def test_sweep_isolates_per_customer_failure(monkeypatch):
    """A customer whose verify raises must not abort the rest of the sweep."""
    customers = [
        SimpleNamespace(id=uuid.uuid4(), broker="bbi", username="boom"),
        SimpleNamespace(id=uuid.uuid4(), broker="bbi", username="good"),
    ]
    verify_results = {"good": VerifyResult(ok=True, status=CredStatus.VALID)}
    recorder: list = []
    _patch_common(monkeypatch, customers, verify_results, recorder)

    # "boom" isn't in verify_results → KeyError inside the per-customer try.
    checked, valid, invalid = await credential_checker._sweep_once()

    assert checked == 1 and valid == 1  # only "good" completed
    assert dict(recorder) == {customers[1].id: CredStatus.VALID}


# --------------------------------------------------------------------------
# recheck_transients — the self-heal mechanism for rate-limit casualties
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recheck_transients_resolves_and_stops(monkeypatch):
    """A transient that now verifies VALID is overwritten, and the loop stops as
    soon as nothing is still transient. The cooldown is awaited via the injected
    (no-op) sleep, not real time."""
    cust = SimpleNamespace(id=uuid.uuid4(), broker="bbi", username="flaky")
    recorder: list = []
    _patch_common(
        monkeypatch, [cust],
        {"flaky": VerifyResult(ok=True, status=CredStatus.VALID)}, recorder,
    )
    from app.services import customers as customers_svc
    monkeypatch.setattr(customers_svc, "list_customers", AsyncMock(return_value=[cust]))
    sleeps: list = []

    async def _sleep(s):
        sleeps.append(s)

    out = await credential_checker.recheck_transients(
        rounds=3, cooldown_seconds=300, pace_seconds=1.5, sleep_fn=_sleep
    )
    assert out == {"rounds": 1, "resolved": 1}  # resolved in round 1, then break
    assert dict(recorder)[cust.id] is CredStatus.VALID
    assert 300 in sleeps  # cooldown was awaited before the recheck


@pytest.mark.asyncio
async def test_recheck_transients_is_bounded(monkeypatch):
    """A still-transient account is retried EXACTLY `rounds` times, never forever."""
    cust = SimpleNamespace(id=uuid.uuid4(), broker="bbi", username="stuck")
    recorder: list = []
    _patch_common(
        monkeypatch, [cust],
        {"stuck": VerifyResult(ok=False, status=CredStatus.TRANSIENT)}, recorder,
    )
    from app.services import customers as customers_svc
    monkeypatch.setattr(customers_svc, "list_customers", AsyncMock(return_value=[cust]))

    async def _sleep(_s):
        pass

    out = await credential_checker.recheck_transients(
        rounds=3, cooldown_seconds=0, pace_seconds=0, sleep_fn=_sleep
    )
    assert out == {"rounds": 3, "resolved": 0}


@pytest.mark.asyncio
async def test_sweep_invokes_retry_when_configured(monkeypatch):
    """_sweep_once with retry_rounds>0 kicks off recheck_transients after the
    full pass; with 0 it doesn't."""
    cust = SimpleNamespace(id=uuid.uuid4(), broker="bbi", username="flaky")
    recorder: list = []
    _patch_common(
        monkeypatch, [cust],
        {"flaky": VerifyResult(ok=False, status=CredStatus.TRANSIENT)}, recorder,
    )
    calls: list = []

    async def _recheck(**kw):
        calls.append(kw)
        return {"rounds": kw["rounds"], "resolved": 0}

    monkeypatch.setattr(credential_checker, "recheck_transients", _recheck)

    async def _sleep(_s):
        pass

    await credential_checker._sweep_once(
        pace_seconds=0, retry_rounds=2, retry_cooldown_seconds=99, sleep_fn=_sleep
    )
    assert len(calls) == 1 and calls[0]["rounds"] == 2 and calls[0]["cooldown_seconds"] == 99

    calls.clear()
    await credential_checker._sweep_once(retry_rounds=0)
    assert calls == []
