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


def test_customer_create_accepts_any_nonempty_broker_string() -> None:
    """``broker`` is now a free string — the source of truth is the
    ``brokers`` DB table, checked in the service layer
    (``customers._validate_broker``), NOT a closed Pydantic ``Literal``.

    So the schema accepts any non-empty code (e.g. a DB-managed Exir tenant
    or operator-added broker); an unknown/disabled code is rejected later
    with a plain ``ValueError`` by the service, not a 422 here.
    """
    kwargs = _base_create_kwargs()
    kwargs["broker"] = "khobregan"  # an Exir tenant code — unknown to the old Literal
    model = CustomerCreate(**kwargs)
    assert model.broker == "khobregan"


def test_customer_create_rejects_empty_broker() -> None:
    """An empty broker string is still a schema error (min_length=1)."""
    kwargs = _base_create_kwargs()
    kwargs["broker"] = ""
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
# copy_customer_to_agent (#115)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_customer_to_agent_duplicates_account_and_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy creates a new pending Customer under the target agent with the
    same creds + a fresh TradeInstruction per source row (section_name
    regenerated for the new agent/customer)."""
    from app.models.customers import Customer
    from app.models.trade_instructions import TradeInstruction

    source_id = uuid.uuid4()
    source_agent = uuid.uuid4()
    target_agent = uuid.uuid4()

    source = SimpleNamespace(
        id=source_id,
        agent_id=source_agent,
        broker="bbi",
        username="acct1",
        password_enc=b"cipher",
        display_name="Acme",
    )

    async def _fake_get(_db, _cid):
        return source

    monkeypatch.setattr(customers_svc, "get_customer", _fake_get)

    src_tis = [
        SimpleNamespace(isin="IRO1AAA0001", side=1, comment="buy a",
                        auto_sell_threshold=None, auto_sell_only=False),
        SimpleNamespace(isin="IRO1BBB0001", side=2, comment=None,
                        auto_sell_threshold=None, auto_sell_only=False),
    ]

    added: list = []

    def _add(obj):
        added.append(obj)

    async def _flush():
        # Simulate Postgres server-default id assignment on insert/flush.
        for obj in added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    ti_result = MagicMock()
    ti_result.scalars.return_value.all.return_value = src_tis

    db = MagicMock()
    db.add = MagicMock(side_effect=_add)
    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(return_value=ti_result)  # the source-TI select
    db.get = AsyncMock(
        return_value=SimpleNamespace(id=target_agent, deleted_at=None)
    )  # the target User lookup

    new_customer = await customers_svc.copy_customer_to_agent(
        db, source_id, target_agent_id=target_agent, actor_id=uuid.uuid4()
    )

    assert isinstance(new_customer, Customer)
    assert new_customer.agent_id == target_agent
    assert new_customer.broker == "bbi"
    assert new_customer.username == "acct1"
    assert new_customer.password_enc == b"cipher"
    assert new_customer.assignment_status == "pending"
    assert new_customer.server_id is None and new_customer.stack_id is None

    new_tis = [o for o in added if isinstance(o, TradeInstruction)]
    assert len(new_tis) == 2
    for nti in new_tis:
        assert nti.customer_id == new_customer.id
        # section_name embeds the TARGET agent's id, not the source's.
        assert nti.section_name.startswith(f"a{target_agent.hex[:8]}_")
        assert nti.section_name != ""
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_copy_customer_preserves_auto_sell_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the copy used to drop ``auto_sell_threshold`` (and would
    have dropped ``auto_sell_only``) — turning a copied watch-only instruction
    into a plain Buy that FIRES at open. Both must carry over verbatim."""
    from app.models.trade_instructions import TradeInstruction

    source_id = uuid.uuid4()
    target_agent = uuid.uuid4()

    source = SimpleNamespace(
        id=source_id,
        agent_id=uuid.uuid4(),
        broker="bbi",
        username="acct1",
        password_enc=b"cipher",
        display_name="Acme",
    )

    async def _fake_get(_db, _cid):
        return source

    monkeypatch.setattr(customers_svc, "get_customer", _fake_get)

    src_tis = [
        SimpleNamespace(isin="IRO1AAA0001", side=1, comment="armed",
                        auto_sell_threshold=750, auto_sell_only=True),
    ]

    added: list = []

    def _add(obj):
        added.append(obj)

    async def _flush():
        for obj in added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    ti_result = MagicMock()
    ti_result.scalars.return_value.all.return_value = src_tis

    db = MagicMock()
    db.add = MagicMock(side_effect=_add)
    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(return_value=ti_result)
    db.get = AsyncMock(
        return_value=SimpleNamespace(id=target_agent, deleted_at=None)
    )

    await customers_svc.copy_customer_to_agent(
        db, source_id, target_agent_id=target_agent, actor_id=uuid.uuid4()
    )

    new_tis = [o for o in added if isinstance(o, TradeInstruction)]
    assert len(new_tis) == 1
    assert new_tis[0].auto_sell_threshold == 750
    assert new_tis[0].auto_sell_only is True


@pytest.mark.asyncio
async def test_copy_customer_to_same_agent_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = uuid.uuid4()
    source = SimpleNamespace(
        id=uuid.uuid4(), agent_id=agent, broker="bbi",
        username="u", password_enc=b"", display_name="x",
    )

    async def _fake_get(_db, _cid):
        return source

    monkeypatch.setattr(customers_svc, "get_customer", _fake_get)
    with pytest.raises(ValueError):
        await customers_svc.copy_customer_to_agent(
            MagicMock(), source.id, target_agent_id=agent, actor_id=uuid.uuid4()
        )


@pytest.mark.asyncio
async def test_copy_customer_missing_source_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _none(_db, _cid):
        return None

    monkeypatch.setattr(customers_svc, "get_customer", _none)
    with pytest.raises(LookupError):
        await customers_svc.copy_customer_to_agent(
            MagicMock(), uuid.uuid4(), target_agent_id=uuid.uuid4(), actor_id=uuid.uuid4()
        )


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
