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
