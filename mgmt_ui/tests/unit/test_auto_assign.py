"""Unit tests for auto-assigning an agent-created customer to a RANDOM EXISTING
stack of that agent (no stack creation; stays pending if the agent has none).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services import distribution as dist


def _fake_customer(agent_id):
    return SimpleNamespace(
        id=uuid4(), agent_id=agent_id, server_id=None, stack_id=None,
        assignment_status="pending", broker="bbi", username="u1", updated_at=None,
    )


def _stacks_result(rows):
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


async def test_auto_assign_picks_an_existing_stack(monkeypatch):
    agent = uuid4()
    cust = _fake_customer(agent)
    st1 = SimpleNamespace(id=uuid4(), server_id=uuid4(), agent_id=agent)
    st2 = SimpleNamespace(id=uuid4(), server_id=uuid4(), agent_id=agent)

    monkeypatch.setattr(dist, "_lock_customer_for_update", AsyncMock(return_value=cust))
    monkeypatch.setattr(dist.random, "choice", lambda seq: seq[1])  # deterministic pick
    fake_stacks = MagicMock()
    fake_stacks.push_config_ini_for_stack = AsyncMock()
    fake_stacks.find_or_create_stack = AsyncMock()  # must NOT be called
    monkeypatch.setattr(dist, "_import_stacks_service", lambda: fake_stacks)

    db = MagicMock()
    db.execute = AsyncMock(return_value=_stacks_result([st1, st2]))
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    res = await dist.assign_customer_to_random_existing_stack(db, cust.id, actor_id=uuid4())

    assert res.ok is True
    assert cust.stack_id == st2.id          # the chosen existing stack
    assert cust.server_id == st2.server_id
    assert cust.assignment_status == "active"
    fake_stacks.find_or_create_stack.assert_not_called()   # NO stack creation
    fake_stacks.push_config_ini_for_stack.assert_awaited_once()
    db.commit.assert_awaited()


async def test_auto_assign_no_stack_leaves_pending(monkeypatch):
    agent = uuid4()
    cust = _fake_customer(agent)

    monkeypatch.setattr(dist, "_lock_customer_for_update", AsyncMock(return_value=cust))
    fake_stacks = MagicMock()
    fake_stacks.push_config_ini_for_stack = AsyncMock()
    monkeypatch.setattr(dist, "_import_stacks_service", lambda: fake_stacks)

    db = MagicMock()
    db.execute = AsyncMock(return_value=_stacks_result([]))   # agent has NO stacks
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    res = await dist.assign_customer_to_random_existing_stack(db, cust.id, actor_id=uuid4())

    assert res.ok is False
    assert cust.assignment_status == "pending"   # untouched
    assert cust.stack_id is None
    fake_stacks.push_config_ini_for_stack.assert_not_awaited()


async def test_auto_assign_unknown_customer_raises(monkeypatch):
    monkeypatch.setattr(dist, "_lock_customer_for_update", AsyncMock(return_value=None))
    db = MagicMock()
    with pytest.raises(ValueError):
        await dist.assign_customer_to_random_existing_stack(db, uuid4(), actor_id=uuid4())
