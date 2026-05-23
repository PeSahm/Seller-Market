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

from app.schemas.trade_instruction import (
    TradeInstructionCreate,
    TradeInstructionOut,
    TradeInstructionUpdate,
)
from app.services import trade_instructions as ti_svc
from app.services.trade_instructions import (
    OptimisticLockError,
    _build_section_name,
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
            "enabled", "comment", "version"} <= fields


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
        enabled=True,
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
