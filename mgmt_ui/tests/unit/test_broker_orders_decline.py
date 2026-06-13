"""Unit tests for the per-order fee decline service (#fee-decline).

decline_order / undo_decline set/clear ``fee_excluded_at``/``fee_excluded_by``,
write one audit row, and enforce the agent guardrail (only a time-window-guess
buy is agent-declinable). No DB — a fake session with get/add/commit.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.models.audit import AuditLog
from app.models.broker_orders import BrokerOrder
from app.services import broker_orders as svc


class _FakeDB:
    def __init__(self, order):
        self._order = order
        self.added: list = []
        self.commits = 0

    async def get(self, _model, _pk):
        return self._order

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _bo(*, side=1, is_bot=False, declined=False) -> BrokerOrder:
    o = BrokerOrder(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        isin="IRO1XXXX0001",
        tracking_number=7,
        order_side=side,
        is_bot=is_bot,
    )
    if declined:
        o.fee_excluded_at = datetime.now(timezone.utc)
        o.fee_excluded_by = uuid.uuid4()
    return o


def _audits(db):
    return [a for a in db.added if isinstance(a, AuditLog)]


async def test_decline_admin_confirmed_bot_succeeds():
    actor = uuid.uuid4()
    order = _bo(side=1, is_bot=True)  # confirmed bot — only admin may decline
    db = _FakeDB(order)
    out = await svc.decline_order(db, order.id, actor_id=actor, allow_confirmed_bot=True)
    assert out.fee_excluded_at is not None
    assert out.fee_excluded_by == actor
    aus = _audits(db)
    assert len(aus) == 1
    assert aus[0].action == "broker_order.decline"
    assert aus[0].target_type == "broker_order"
    assert aus[0].target_id == str(order.id)
    assert db.commits == 1


async def test_decline_agent_window_guess_succeeds():
    order = _bo(side=1, is_bot=False)  # window-guess buy — agent-declinable
    db = _FakeDB(order)
    await svc.decline_order(db, order.id, actor_id=uuid.uuid4(), allow_confirmed_bot=False)
    assert order.fee_excluded_at is not None
    assert db.commits == 1


async def test_decline_agent_confirmed_bot_rejected():
    order = _bo(side=1, is_bot=True)
    db = _FakeDB(order)
    with pytest.raises(svc.DeclineNotAllowedError):
        await svc.decline_order(
            db, order.id, actor_id=uuid.uuid4(), allow_confirmed_bot=False
        )
    assert order.fee_excluded_at is None
    assert _audits(db) == []
    assert db.commits == 0


async def test_decline_agent_sell_rejected():
    order = _bo(side=2, is_bot=False)
    db = _FakeDB(order)
    with pytest.raises(svc.DeclineNotAllowedError):
        await svc.decline_order(
            db, order.id, actor_id=uuid.uuid4(), allow_confirmed_bot=False
        )
    assert db.commits == 0


async def test_decline_unknown_order_raises():
    db = _FakeDB(None)
    with pytest.raises(svc.OrderNotFoundError):
        await svc.decline_order(
            db, uuid.uuid4(), actor_id=uuid.uuid4(), allow_confirmed_bot=True
        )


async def test_decline_idempotent_no_second_audit():
    order = _bo(side=1, is_bot=False, declined=True)
    db = _FakeDB(order)
    await svc.decline_order(db, order.id, actor_id=uuid.uuid4(), allow_confirmed_bot=False)
    assert _audits(db) == []  # already declined — no duplicate audit
    assert db.commits == 0


async def test_undo_clears_and_audits():
    order = _bo(side=1, is_bot=False, declined=True)
    db = _FakeDB(order)
    await svc.undo_decline(db, order.id, actor_id=uuid.uuid4())
    assert order.fee_excluded_at is None
    assert order.fee_excluded_by is None
    aus = _audits(db)
    assert len(aus) == 1 and aus[0].action == "broker_order.undo_decline"
    assert db.commits == 1


async def test_undo_noop_when_active():
    order = _bo(side=1, is_bot=False, declined=False)
    db = _FakeDB(order)
    await svc.undo_decline(db, order.id, actor_id=uuid.uuid4())
    assert _audits(db) == []
    assert db.commits == 0
