"""Unit tests for the Customer service (post-migration 0003).

Pure logic tests — no live DB, no SSH. Customer is now account-shaped;
the per-instrument fields (isin/side/comment/section_name) moved to
TradeInstruction (see ``test_trade_instructions.py`` for those).

Nothing in this module imports ``app.services.ssh`` or ``httpx`` — the
schema/service split MUST stay free of those for the conftest's dev-mode
Fernet placeholder to be enough.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.schemas.customer import (
    BROKERS,
    CustomerCreate,
    CustomerOut,
    CustomerUpdate,
)
from app.services import customers as customers_svc
from app.services.customers import (
    OptimisticLockError,
    list_customers,
    update_customer,
)


# ---------------------------------------------------------------------------
# CustomerCreate / CustomerUpdate schema sanity
# ---------------------------------------------------------------------------


def _base_create_kwargs() -> dict:
    """Minimal valid kwargs for ``CustomerCreate`` — tests vary one field."""
    return {
        "display_name": "Test Account",
        "broker": "bbi",
        "username": "myuser",
        "password": "s3cret",
    }


def test_customer_create_minimal() -> None:
    """Happy path — the account-shaped fields suffice."""
    model = CustomerCreate(**_base_create_kwargs())
    assert model.display_name == "Test Account"
    assert model.broker == "bbi"
    assert model.username == "myuser"
    assert model.password == "s3cret"


def test_customer_create_rejects_invalid_broker() -> None:
    """``broker`` is a strict Literal — anything outside the tuple is 422."""
    kwargs = _base_create_kwargs()
    kwargs["broker"] = "totallybogus"
    with pytest.raises(ValidationError):
        CustomerCreate(**kwargs)


def test_customer_create_persian_display_name_roundtrip() -> None:
    """Non-ASCII display names must round-trip without mangling."""
    kwargs = _base_create_kwargs()
    kwargs["display_name"] = "حسین شفن"
    model = CustomerCreate(**kwargs)
    assert model.display_name == "حسین شفن"


def test_customer_create_no_longer_accepts_isin_side() -> None:
    """The migration moved isin/side to TradeInstruction — they're no
    longer accepted on CustomerCreate. Pydantic ignores unknown fields
    by default; we only care that the model still builds without them
    AND that the model doesn't quietly accept them as extras."""
    CustomerCreate(**_base_create_kwargs())  # builds cleanly without them
    assert "isin" not in CustomerCreate.model_fields
    assert "side" not in CustomerCreate.model_fields
    assert "comment" not in CustomerCreate.model_fields


# ---------------------------------------------------------------------------
# CustomerOut hygiene
# ---------------------------------------------------------------------------


def test_customer_out_excludes_password_enc() -> None:
    """CustomerOut must not declare ``password_enc`` (Fernet ciphertext)
    or ``password`` — secrets must not round-trip through HTML."""
    field_names = set(CustomerOut.model_fields.keys())
    assert "password_enc" not in field_names
    assert "password" not in field_names
    # Sanity-check the safe fields are present.
    assert {"id", "agent_id", "broker", "username", "version"} <= field_names


def test_customer_out_no_longer_carries_per_trade_fields() -> None:
    """The migration moved isin/side/section_name/comment to TradeInstruction —
    they must NOT appear on CustomerOut."""
    field_names = set(CustomerOut.model_fields.keys())
    for missing in ("isin", "side", "section_name", "comment"):
        assert missing not in field_names, (
            f"{missing!r} unexpectedly present on CustomerOut after 0003"
        )


# ---------------------------------------------------------------------------
# Optimistic-lock branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_optimistic_lock_version_mismatch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``update_customer`` raises ``OptimisticLockError`` on version mismatch.

    Stubs :func:`app.services.customers.get_customer` so the test doesn't
    need a real SQLAlchemy session. ``db.flush`` and ``db.commit`` must
    NOT be called on the mismatch path.
    """
    db = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    fake_row = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        version=5,
        display_name="x",
        broker="bbi",
        username="u",
        password_enc=b"",
        server_id=None,
        stack_id=None,
        assignment_status="pending",
    )

    async def _fake_get(_db, _cid):
        return fake_row

    monkeypatch.setattr(customers_svc, "get_customer", _fake_get)

    update = CustomerUpdate(version=4, display_name="renamed")

    with pytest.raises(OptimisticLockError):
        await update_customer(db, fake_row.id, update, actor_id=uuid.uuid4())

    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Search by name / username (the operator-requested feature)
# ---------------------------------------------------------------------------


def test_escape_ilike_escapes_wildcards() -> None:
    """``%`` and ``_`` in user input get backslash-escaped so a literal
    percent doesn't match everything in the table."""
    assert customers_svc._escape_ilike("ali") == "ali"
    assert customers_svc._escape_ilike("100%") == "100\\%"
    assert customers_svc._escape_ilike("a_b") == "a\\_b"
    # Backslash itself escapes to backslash-backslash.
    assert customers_svc._escape_ilike("a\\b") == "a\\\\b"


# ---------------------------------------------------------------------------
# Broker tuple ↔ Literal sync
# ---------------------------------------------------------------------------


def test_brokers_tuple_matches_broker_enum_codes() -> None:
    """The schema's BROKERS tuple stays in sync with the canonical enum.

    Skipped on dev boxes where the legacy ``SellerMarket`` package isn't on
    ``sys.path`` — it's deliberately not a runtime dependency of mgmt_ui.
    """
    try:
        from SellerMarket.broker_enum import BrokerCode  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        pytest.skip("SellerMarket package not importable from this venv")

    enum_codes = {b.value for b in BrokerCode}
    schema_codes = set(BROKERS)
    assert enum_codes == schema_codes
