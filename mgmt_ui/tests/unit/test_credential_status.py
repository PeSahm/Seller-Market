"""Unit tests for customers.set_credential_status (sticky-transient rule).

No live DB — get_customer is stubbed and the session is a mock, mirroring
test_customers.py's optimistic-lock test.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import customers as customers_svc
from app.services.customers import set_credential_status
from app.services.brokers.base import CredStatus


def _fake_db() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _fake_customer(cred_status: str = "unknown") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        server_id=None,
        stack_id=None,
        assignment_status="active",
        display_name="x",
        username="u",
        broker="bbi",
        fee_percent=None,
        credential_status=cred_status,
        credential_checked_at=None,
        credential_message=None,
        version=7,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (CredStatus.VALID, "valid"),
        (CredStatus.INVALID_CREDENTIALS, "invalid"),
        (CredStatus.TRANSIENT, "transient"),
    ],
)
async def test_set_status_from_unknown(monkeypatch, status, expected):
    cust = _fake_customer("unknown")
    monkeypatch.setattr(customers_svc, "get_customer", AsyncMock(return_value=cust))
    await set_credential_status(_fake_db(), cust.id, status, "msg")
    assert cust.credential_status == expected
    assert cust.credential_checked_at is not None
    assert cust.credential_message == "msg"
    # system metadata — never bumps the optimistic-lock version
    assert cust.version == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["valid", "invalid"])
async def test_transient_is_sticky_over_real_verdict(monkeypatch, prior):
    """A TRANSIENT result (OCR/broker down) must NOT downgrade a known
    valid/invalid verdict — only the timestamp/message refresh."""
    cust = _fake_customer(prior)
    monkeypatch.setattr(customers_svc, "get_customer", AsyncMock(return_value=cust))
    await set_credential_status(_fake_db(), cust.id, CredStatus.TRANSIENT, "ocr down")
    assert cust.credential_status == prior  # unchanged
    assert cust.credential_checked_at is not None
    assert cust.credential_message == "ocr down"


@pytest.mark.asyncio
async def test_valid_overrides_prior_invalid(monkeypatch):
    """A fresh VALID verdict clears a prior invalid (agent fixed the password)."""
    cust = _fake_customer("invalid")
    monkeypatch.setattr(customers_svc, "get_customer", AsyncMock(return_value=cust))
    await set_credential_status(_fake_db(), cust.id, CredStatus.VALID)
    assert cust.credential_status == "valid"


@pytest.mark.asyncio
async def test_message_truncated_to_512(monkeypatch):
    cust = _fake_customer("unknown")
    monkeypatch.setattr(customers_svc, "get_customer", AsyncMock(return_value=cust))
    await set_credential_status(_fake_db(), cust.id, CredStatus.VALID, "x" * 999)
    assert len(cust.credential_message) == 512


@pytest.mark.asyncio
async def test_missing_customer_raises(monkeypatch):
    monkeypatch.setattr(customers_svc, "get_customer", AsyncMock(return_value=None))
    with pytest.raises(LookupError):
        await set_credential_status(_fake_db(), uuid.uuid4(), CredStatus.VALID)


def test_resolve_cred_status():
    from app.services.brokers.base import VerifyResult, resolve_cred_status
    assert resolve_cred_status(VerifyResult(ok=True)) is CredStatus.VALID
    assert resolve_cred_status(
        VerifyResult(ok=False, status=CredStatus.INVALID_CREDENTIALS)
    ) is CredStatus.INVALID_CREDENTIALS
    assert resolve_cred_status(VerifyResult(ok=False)) is CredStatus.TRANSIENT
