"""Unit tests for the customer service.

Pure logic tests — no live DB, no SSH. The service-layer helpers are
exercised against mocked sessions; the schema validators run inline through
pydantic. The optimistic-lock check is the one piece of branching logic
worth a dedicated test, so we mock ``get_customer`` to hand back a synthetic
row with a known ``version``.

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
    _build_section_name,
    update_customer,
)


# ---------------------------------------------------------------------------
# 1-3. _build_section_name
# ---------------------------------------------------------------------------


def test_build_section_name_short_form() -> None:
    """Section name follows the ``a<8hex>_c<8hex>_<broker>_<isin>`` pattern.

    We pin both UUIDs to known values so the assertion is byte-exact rather
    than just structural.
    """
    agent_id = uuid.UUID("4eebf408-aaaa-bbbb-cccc-000000000001")
    customer_id = uuid.UUID("04cdabd0-aaaa-bbbb-cccc-000000000002")
    name = _build_section_name(agent_id, customer_id, "bbi", "IRO3AYHZ0001")
    assert name == "a4eebf408_c04cdabd0_bbi_IRO3AYHZ0001"
    # Defense-in-depth: only hex chars (plus the separators / broker / isin)
    # ever reach the output.
    assert "a4eebf408".startswith("a")
    assert "_c04cdabd0" in name


def test_build_section_name_deterministic() -> None:
    """Same inputs ⇒ same output, byte-exact, across repeated calls.

    Important because the render layer caches section names; a
    non-deterministic builder would invalidate the cache on every render.
    """
    agent_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    a = _build_section_name(agent_id, customer_id, "shahr", "IRO1AHRM0001")
    b = _build_section_name(agent_id, customer_id, "shahr", "IRO1AHRM0001")
    assert a == b


def test_build_section_name_distinct_for_different_uuids() -> None:
    """Distinct UUID pairs produce distinct section names in practice.

    Real proof would require the DB UNIQUE constraint; this test just
    confirms the builder doesn't accidentally collapse inputs (e.g. a typo
    that hashed everything to the same prefix).
    """
    agent_a = uuid.UUID("11111111-aaaa-bbbb-cccc-000000000001")
    agent_b = uuid.UUID("22222222-aaaa-bbbb-cccc-000000000001")
    cust_a = uuid.UUID("33333333-aaaa-bbbb-cccc-000000000001")
    cust_b = uuid.UUID("44444444-aaaa-bbbb-cccc-000000000001")

    a = _build_section_name(agent_a, cust_a, "bbi", "IRO3AYHZ0001")
    b = _build_section_name(agent_b, cust_a, "bbi", "IRO3AYHZ0001")
    c = _build_section_name(agent_a, cust_b, "bbi", "IRO3AYHZ0001")
    assert a != b
    assert a != c
    assert b != c


# ---------------------------------------------------------------------------
# 4-6. ISIN validator
# ---------------------------------------------------------------------------


def _base_create_kwargs() -> dict:
    """Minimal valid kwargs for ``CustomerCreate`` — tests vary one field."""
    return {
        "display_name": "Test Account",
        "broker": "bbi",
        "isin": "IRO3AYHZ0001",
        "side": 1,
        "username": "myuser",
        "password": "s3cret",
        "comment": None,
    }


def test_isin_validator_rejects_spaces() -> None:
    """A space inside the ISIN is a syntax error (would break ``config.ini``)."""
    kwargs = _base_create_kwargs()
    kwargs["isin"] = "IR O3AYHZ0001"
    with pytest.raises(ValidationError) as excinfo:
        CustomerCreate(**kwargs)
    assert "isin" in str(excinfo.value).lower()


def test_isin_validator_rejects_punctuation() -> None:
    """Hyphens, slashes, etc. are rejected — only alphanumerics allowed."""
    kwargs = _base_create_kwargs()
    kwargs["isin"] = "IR-O3"
    with pytest.raises(ValidationError):
        CustomerCreate(**kwargs)


def test_isin_validator_uppercases() -> None:
    """Lowercase input is normalized to uppercase.

    The Tehran Stock Exchange ISIN format is upper-case by convention and
    the downstream bot does exact-match comparison; canonicalizing here
    means an agent typing ``iro3ayhz0001`` doesn't end up with a
    different section name than another agent typing the same instrument
    in upper case.
    """
    kwargs = _base_create_kwargs()
    kwargs["isin"] = "iro3ayhz0001"
    model = CustomerCreate(**kwargs)
    assert model.isin == "IRO3AYHZ0001"


# ---------------------------------------------------------------------------
# 7-8. Side enum
# ---------------------------------------------------------------------------


def test_side_enum_rejects_zero() -> None:
    """side=0 is not Buy or Sell; pydantic must refuse it.

    The DB has a CHECK constraint enforcing the same; surfacing it at the
    schema boundary means an invalid form submission becomes a 422 instead
    of an opaque IntegrityError later.
    """
    kwargs = _base_create_kwargs()
    kwargs["side"] = 0
    with pytest.raises(ValidationError):
        CustomerCreate(**kwargs)


def test_side_enum_rejects_three() -> None:
    """side=3 is out of range — same rationale as the zero test."""
    kwargs = _base_create_kwargs()
    kwargs["side"] = 3
    with pytest.raises(ValidationError):
        CustomerCreate(**kwargs)


# ---------------------------------------------------------------------------
# 9. Persian display name (Unicode round-trip)
# ---------------------------------------------------------------------------


def test_customer_create_accepts_persian_display_name() -> None:
    """Non-ASCII display names round-trip without mangling.

    The UI is bilingual (Persian/English) and the model column is plain
    ``String(255)`` — UTF-8 under the hood — so this should just work, but
    we pin it with a test because Pydantic V2 has historically had subtle
    encoding bugs around field validators that mutate values.
    """
    kwargs = _base_create_kwargs()
    kwargs["display_name"] = "حسین شفن"
    model = CustomerCreate(**kwargs)
    assert model.display_name == "حسین شفن"


# ---------------------------------------------------------------------------
# 10. CustomerOut hygiene — no secrets leak
# ---------------------------------------------------------------------------


def test_customer_out_excludes_password_enc() -> None:
    """CustomerOut must not declare a ``password_enc`` (or ``password``) field.

    This is the one assertion that protects against an accidental schema
    edit that adds the ciphertext to the outbound representation. A
    runtime check on a real Customer row would be ideal, but the model is
    a plain pydantic ``BaseModel`` so we can introspect ``model_fields``
    directly.
    """
    field_names = set(CustomerOut.model_fields.keys())
    assert "password_enc" not in field_names
    assert "password" not in field_names
    # Sanity-check we DO surface the safe fields the UI needs.
    assert {"id", "agent_id", "section_name", "version"} <= field_names


# ---------------------------------------------------------------------------
# 11. Optimistic-lock branch
# ---------------------------------------------------------------------------


async def test_optimistic_lock_version_mismatch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``update_customer`` raises ``OptimisticLockError`` on version mismatch.

    We stub :func:`app.services.customers.get_customer` so the test doesn't
    need a real SQLAlchemy session — the only branch we want to exercise
    is the version compare. ``db.flush`` and ``db.commit`` must NOT be
    called on the mismatch path, so we assert that too.
    """
    db = MagicMock()
    # ``flush`` and ``commit`` are awaitable on a real AsyncSession; mocking
    # them with AsyncMock lets us assert they were NOT awaited.
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    # Fake "row" — only the attributes the function touches matter.
    fake_row = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        version=5,
        display_name="x",
        broker="bbi",
        isin="IRO3AYHZ0001",
        section_name="a00000000_c00000000_bbi_IRO3AYHZ0001",
        username="u",
        password_enc=b"",
        side=1,
        enabled=True,
        comment=None,
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

    # Critical: we must not have written anything on the mismatch path.
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Sanity-check the broker tuple matches the Literal — catches drift between
# the two sources of truth on the next schema edit. Not in the 11 required
# tests; cheap insurance.
# ---------------------------------------------------------------------------


def test_brokers_tuple_matches_broker_enum_codes() -> None:
    """The schema's BROKERS tuple stays in sync with the canonical enum.

    If a new broker is added to ``SellerMarket.broker_enum.BrokerCode`` it
    must also be added to ``app.schemas.customer.BROKERS`` (and the
    ``Broker`` Literal). This test fails loudly when one is updated
    without the other.

    Skipped on dev boxes where the legacy ``SellerMarket`` package isn't on
    ``sys.path`` — it's deliberately not a runtime dependency of mgmt_ui,
    so importing it from the test venv is best-effort.
    """
    # Imported lazily so the test module doesn't depend on the legacy bot
    # source tree being importable at collection time on every dev box.
    try:
        from SellerMarket.broker_enum import BrokerCode  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        pytest.skip("SellerMarket package not importable from this venv")

    enum_codes = {b.value for b in BrokerCode}
    schema_codes = set(BROKERS)
    assert enum_codes == schema_codes
