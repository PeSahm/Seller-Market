"""Unit tests for the TradeInstruction service (new in 0003).

Mirror the shape of test_customers.py — schema validators, optimistic-
lock branch, section-name builder. No DB, no SSH.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.models.audit import AuditLog
from app.models.trade_instructions import TradeInstruction
from app.schemas.trade_instruction import (
    TradeInstructionCreate,
    TradeInstructionOut,
    TradeInstructionUpdate,
    map_side_form,
)
from app.services import trade_instructions as ti_svc
from app.services.trade_instructions import (
    OptimisticLockError,
    _build_section_name,
    bulk_create_trade_instruction,
    update_trade_instruction,
)

# ---------------------------------------------------------------------------
# section_name builder
# ---------------------------------------------------------------------------


def test_build_section_name_format() -> None:
    """Format: a<8h>_c<8h>_t<8h>_<broker>_<isin>_s<side>."""
    agent_id = uuid.UUID("4eebf408-aaaa-bbbb-cccc-000000000001")
    customer_id = uuid.UUID("04cdabd0-aaaa-bbbb-cccc-000000000002")
    trade_id = uuid.UUID("0bcafe00-aaaa-bbbb-cccc-000000000003")
    name = _build_section_name(
        agent_id, customer_id, trade_id, "bbi", "IRO3AYHZ0001", 1
    )
    assert name == "a4eebf408_c04cdabd0_t0bcafe00_bbi_IRO3AYHZ0001_s1"


def test_build_section_name_deterministic() -> None:
    """Same inputs ⇒ same output (the renderer depends on this)."""
    a, c, t = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    n1 = _build_section_name(a, c, t, "shahr", "IRO1AHRM0001", 2)
    n2 = _build_section_name(a, c, t, "shahr", "IRO1AHRM0001", 2)
    assert n1 == n2


def test_build_section_name_disambiguates_same_isin_side() -> None:
    """Two trades with the same broker/isin/side under different
    customers must get different section names (the t<...> slice is
    what carries the uniqueness)."""
    a = uuid.uuid4()
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    n1 = _build_section_name(a, c1, t1, "bbi", "IRO3AYHZ0001", 1)
    n2 = _build_section_name(a, c2, t2, "bbi", "IRO3AYHZ0001", 1)
    assert n1 != n2


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


def test_isin_validator_rejects_punctuation() -> None:
    with pytest.raises(ValidationError):
        TradeInstructionCreate(isin="IR-O3", side=1)


def test_isin_validator_uppercases() -> None:
    m = TradeInstructionCreate(isin="iro3ayhz0001", side=1)
    assert m.isin == "IRO3AYHZ0001"


def test_side_enum_rejects_three() -> None:
    with pytest.raises(ValidationError):
        TradeInstructionCreate(isin="IRO3AYHZ0001", side=3)


def test_trade_instruction_out_has_required_fields() -> None:
    fields = set(TradeInstructionOut.model_fields.keys())
    assert {"id", "customer_id", "isin", "side", "section_name",
            "comment", "version", "auto_sell_threshold",
            "auto_sell_only"} <= fields


def test_trade_instruction_schemas_drop_enabled() -> None:
    """Migration 0004 dropped the ``enabled`` column — the schemas must
    not silently round-trip it (Pydantic v2's ``extra='ignore'`` would
    otherwise hide an out-of-band write)."""
    assert "enabled" not in TradeInstructionOut.model_fields
    assert "enabled" not in TradeInstructionUpdate.model_fields
    assert "enabled" not in TradeInstructionCreate.model_fields


# ---------------------------------------------------------------------------
# map_side_form (the form-layer side=3 "Auto-sell only" alias)
# ---------------------------------------------------------------------------


def test_map_side_form_matrix() -> None:
    """1 -> (1, False), 2 -> (2, False), 3 -> (1, True) — 3 is never stored."""
    assert map_side_form(1) == (1, False)
    assert map_side_form(2) == (2, False)
    assert map_side_form(3) == (1, True)


@pytest.mark.parametrize("raw", [0, 4])
def test_map_side_form_rejects_out_of_range(raw: int) -> None:
    with pytest.raises(ValueError, match="side must be 1"):
        map_side_form(raw)


# ---------------------------------------------------------------------------
# auto_sell_only Create validator
# ---------------------------------------------------------------------------


def test_create_auto_sell_only_without_threshold_rejected() -> None:
    """A watch-only instruction is meaningless without a trigger threshold."""
    with pytest.raises(ValidationError, match="buy-queue threshold"):
        TradeInstructionCreate(isin="IRO3AYHZ0001", side=1, auto_sell_only=True)


def test_create_auto_sell_only_on_sell_rejected() -> None:
    """The flag is BUY-only — a side=2 row can't be watch-only."""
    with pytest.raises(ValidationError):
        TradeInstructionCreate(
            isin="IRO3AYHZ0001", side=2, auto_sell_only=True,
            auto_sell_threshold=100,
        )


def test_create_auto_sell_only_valid_passes() -> None:
    m = TradeInstructionCreate(
        isin="IRO3AYHZ0001", side=1, auto_sell_only=True,
        auto_sell_threshold=100,
    )
    assert m.auto_sell_only is True
    assert m.auto_sell_threshold == 100
    assert m.side == 1


def test_create_defaults_auto_sell_only_false() -> None:
    """Plain Buys/Sells are untouched — the flag defaults to False."""
    assert TradeInstructionCreate(isin="IRO3AYHZ0001", side=1).auto_sell_only is False


# ---------------------------------------------------------------------------
# Optimistic-lock branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_optimistic_lock_mismatch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    fake_ti = SimpleNamespace(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        version=5,
        isin="IRO3AYHZ0001",
        side=1,
        section_name="a_c_t_bbi_IRO3AYHZ0001_s1",
        comment=None,
    )

    async def _fake_get(_db, _id):
        return fake_ti

    monkeypatch.setattr(ti_svc, "get_trade_instruction", _fake_get)

    update = TradeInstructionUpdate(version=4, comment="changed")

    with pytest.raises(OptimisticLockError):
        await update_trade_instruction(db, fake_ti.id, update, actor_id=uuid.uuid4())

    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# auto_sell_only effective-state guard on update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_clearing_threshold_on_auto_sell_only_row_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing the threshold on a watch-only row leaves it meaningless —
    the service rejects the EFFECTIVE state (delete the row instead)."""
    db = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    fake_ti = SimpleNamespace(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        version=1,
        isin="IRO3AYHZ0001",
        side=1,
        auto_sell_threshold=500,
        auto_sell_only=True,
        section_name="a_c_t_bbi_IRO3AYHZ0001_s1",
        comment=None,
    )

    async def _fake_get(_db, _id):
        return fake_ti

    monkeypatch.setattr(ti_svc, "get_trade_instruction", _fake_get)

    # 0 normalizes to None → the flagged row would have no trigger left.
    update = TradeInstructionUpdate(version=1, auto_sell_threshold=0)

    with pytest.raises(ValueError, match="buy-queue threshold"):
        await update_trade_instruction(db, fake_ti.id, update, actor_id=uuid.uuid4())

    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_public_snapshot_includes_auto_sell_only() -> None:
    """Audit visibility — the watch-only flag must land in before/after."""
    snap = ti_svc._public_snapshot(
        SimpleNamespace(
            id=uuid.uuid4(),
            customer_id=uuid.uuid4(),
            isin="IRO3AYHZ0001",
            side=1,
            auto_sell_threshold=500,
            auto_sell_only=True,
            section_name="a_c_t_bbi_IRO3AYHZ0001_s1",
            comment=None,
            version=1,
        )
    )
    assert snap["auto_sell_only"] is True
    assert snap["auto_sell_threshold"] == 500


# ---------------------------------------------------------------------------
# delete_all_for_agent (#113)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_all_for_agent_deletes_and_returns_affected_stacks() -> None:
    """Bulk delete returns the count + de-duplicated affected stack ids
    (dropping NULL stack_id for unassigned customers), writes ONE summary
    audit row, and commits."""
    agent_id = uuid.uuid4()
    stack_a = uuid.uuid4()
    # (ti_id, isin, side, stack_id)
    rows = [
        (uuid.uuid4(), "IRO1A", 1, stack_a),
        (uuid.uuid4(), "IRO1B", 2, stack_a),   # same stack → deduped
        (uuid.uuid4(), "IRO1C", 1, None),       # unassigned → dropped
    ]
    select_result = MagicMock()
    select_result.all.return_value = rows

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[select_result, MagicMock()])  # select, then delete
    db.add = MagicMock()
    db.commit = AsyncMock()

    count, stacks = await ti_svc.delete_all_for_agent(
        db, agent_id, actor_id=uuid.uuid4()
    )
    assert count == 3
    assert stacks == [stack_a]
    db.add.assert_called_once()       # single summary audit row
    db.commit.assert_awaited_once()
    assert db.execute.await_count == 2  # the SELECT + the bulk DELETE


@pytest.mark.asyncio
async def test_delete_all_for_agent_empty_is_noop() -> None:
    empty = MagicMock()
    empty.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=empty)
    db.add = MagicMock()
    db.commit = AsyncMock()

    count, stacks = await ti_svc.delete_all_for_agent(
        db, uuid.uuid4(), actor_id=uuid.uuid4()
    )
    assert count == 0 and stacks == []
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# bulk_create_trade_instruction (one instrument → many customers)
# ---------------------------------------------------------------------------


def _bulk_db(cust_rows, existing_ids):
    """Mock AsyncSession for bulk_create: the cust-load SELECT, then the dup
    SELECT. ``flush`` mints an id on each added TradeInstruction so the
    section-name builder has a real UUID to slice."""
    cust_result = MagicMock()
    cust_result.all.return_value = cust_rows
    dup_result = MagicMock()
    dup_result.scalars.return_value.all.return_value = list(existing_ids)

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[cust_result, dup_result])

    added: list = []

    def _add(obj):
        added.append(obj)

    async def _flush():
        for o in added:
            if isinstance(o, TradeInstruction) and getattr(o, "id", None) is None:
                o.id = uuid.uuid4()

    db.add = MagicMock(side_effect=_add)
    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db._added = added
    return db


def _added_tis(db):
    return [o for o in db._added if isinstance(o, TradeInstruction)]


def _added_audits(db):
    return [o for o in db._added if isinstance(o, AuditLog)]


@pytest.mark.asyncio
async def test_bulk_create_all_new_creates_and_dedups_stacks() -> None:
    agent_id = uuid.uuid4()
    c1, c2, c3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stack_a = uuid.uuid4()
    cust_rows = [
        (c1, agent_id, "bbi", stack_a),
        (c2, agent_id, "ayandeh", stack_a),  # same stack → deduped
        (c3, agent_id, "gs", None),          # unassigned → dropped
    ]
    db = _bulk_db(cust_rows, existing_ids=[])
    data = TradeInstructionCreate(isin="IRO3AYHZ0001", side=1)

    res = await bulk_create_trade_instruction(
        db, [c1, c2, c3], data, actor_id=uuid.uuid4()
    )

    assert res.created_count == 3
    assert set(res.created_customer_ids) == {c1, c2, c3}
    assert res.skipped_customer_ids == []
    assert res.affected_stack_ids == [stack_a]  # deduped + NULL dropped
    assert db.add.call_count == 4  # 3 TIs + 1 summary audit
    audits = _added_audits(db)
    assert len(audits) == 1
    assert audits[0].action == "trade_instruction.bulk_create"
    db.commit.assert_awaited_once()
    # Every batched row must carry a DISTINCT, non-empty section_name — a shared
    # "" placeholder would collide on the section_name UNIQUE at INSERT time.
    names = [ti.section_name for ti in _added_tis(db)]
    assert all(names) and len(set(names)) == len(names)


@pytest.mark.asyncio
async def test_bulk_create_mixed_duplicates_skips_some() -> None:
    agent_id = uuid.uuid4()
    c1, c2, c3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stack_a = uuid.uuid4()
    cust_rows = [
        (c1, agent_id, "bbi", stack_a),
        (c2, agent_id, "bbi", stack_a),
        (c3, agent_id, "gs", None),
    ]
    db = _bulk_db(cust_rows, existing_ids=[c2])  # c2 already has (isin, side)
    data = TradeInstructionCreate(isin="IRO3AYHZ0001", side=1)

    res = await bulk_create_trade_instruction(
        db, [c1, c2, c3], data, actor_id=uuid.uuid4()
    )

    assert res.created_count == 2
    assert set(res.created_customer_ids) == {c1, c3}
    assert res.skipped_customer_ids == [c2]
    assert res.affected_stack_ids == [stack_a]
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_bulk_create_all_duplicates_is_noop() -> None:
    agent_id = uuid.uuid4()
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    cust_rows = [
        (c1, agent_id, "bbi", uuid.uuid4()),
        (c2, agent_id, "bbi", uuid.uuid4()),
    ]
    db = _bulk_db(cust_rows, existing_ids=[c1, c2])
    data = TradeInstructionCreate(isin="IRO3AYHZ0001", side=1)

    res = await bulk_create_trade_instruction(
        db, [c1, c2], data, actor_id=uuid.uuid4()
    )

    assert res.created_count == 0
    assert set(res.skipped_customer_ids) == {c1, c2}
    assert res.affected_stack_ids == []
    db.add.assert_not_called()       # no audit row
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()   # honest no-op


@pytest.mark.asyncio
async def test_bulk_create_empty_list_is_noop() -> None:
    db = _bulk_db([], existing_ids=[])
    data = TradeInstructionCreate(isin="IRO3AYHZ0001", side=1)

    res = await bulk_create_trade_instruction(db, [], data, actor_id=uuid.uuid4())

    assert res.created_count == 0
    assert res.created_customer_ids == []
    db.execute.assert_not_awaited()  # never even queried
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bulk_create_foreign_agent_filtered_to_missing() -> None:
    agent_id = uuid.uuid4()
    other_agent = uuid.uuid4()
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    stack_a = uuid.uuid4()
    cust_rows = [
        (c1, agent_id, "bbi", stack_a),
        (c2, other_agent, "gs", stack_a),  # belongs to a different agent
    ]
    db = _bulk_db(cust_rows, existing_ids=[])
    data = TradeInstructionCreate(isin="IRO3AYHZ0001", side=1)

    res = await bulk_create_trade_instruction(
        db, [c1, c2], data, actor_id=uuid.uuid4(), expected_agent_id=agent_id
    )

    assert res.missing_customer_ids == [c2]
    assert res.created_count == 1
    assert set(res.created_customer_ids) == {c1}


@pytest.mark.asyncio
async def test_bulk_create_section_name_per_customer() -> None:
    agent_id = uuid.uuid4()
    c1 = uuid.uuid4()
    cust_rows = [(c1, agent_id, "bbi", uuid.uuid4())]
    db = _bulk_db(cust_rows, existing_ids=[])
    data = TradeInstructionCreate(isin="IRO3AYHZ0001", side=1)

    await bulk_create_trade_instruction(db, [c1], data, actor_id=uuid.uuid4())

    ti = _added_tis(db)[0]
    assert ti.section_name.startswith(f"a{agent_id.hex[:8]}_")
    assert "_bbi_" in ti.section_name
    assert ti.section_name.endswith("_IRO3AYHZ0001_s1")


@pytest.mark.asyncio
async def test_bulk_create_side2_drops_threshold() -> None:
    agent_id = uuid.uuid4()
    c1 = uuid.uuid4()
    cust_rows = [(c1, agent_id, "bbi", uuid.uuid4())]
    db = _bulk_db(cust_rows, existing_ids=[])
    # A Sell with a stray threshold must NOT persist it (auto-sell is BUY-only).
    data = TradeInstructionCreate(isin="IRO3AYHZ0001", side=2, auto_sell_threshold=500)

    await bulk_create_trade_instruction(db, [c1], data, actor_id=uuid.uuid4())

    ti = _added_tis(db)[0]
    assert ti.side == 2
    assert ti.auto_sell_threshold is None


@pytest.mark.asyncio
async def test_agent_bulk_route_rejects_foreign_customer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent POST handler 404s if any posted id isn't the agent's own —
    BEFORE the bulk service is ever invoked."""
    from fastapi import HTTPException

    from app.routers import agent as agent_router

    owned_id = uuid.uuid4()
    foreign_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), role="agent", username="ag")

    async def _list_customers(_db, *, agent_id=None, **_kw):
        return [SimpleNamespace(id=owned_id)]

    monkeypatch.setattr(
        agent_router.services_customers, "list_customers", _list_customers
    )

    async def _never(*_a, **_k):
        raise AssertionError("service must not be called for a foreign id")

    monkeypatch.setattr(
        agent_router.services_trade_instructions,
        "bulk_create_trade_instruction",
        _never,
    )

    db = MagicMock()
    db.refresh = AsyncMock()

    with pytest.raises(HTTPException) as ei:
        await agent_router.agent_bulk_trade_instructions_create(
            request=MagicMock(),
            user=user,
            db=db,
            isin="IRO3AYHZ0001",
            side=1,
            comment=None,
            auto_sell_threshold=None,
            customer_ids=[foreign_id],
        )
    assert ei.value.status_code == 404
