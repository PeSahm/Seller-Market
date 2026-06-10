"""Unit test for the Active-auto-sell page row builder (#110).

The armed-list, the live-queue client, and the order_fires query are mocked.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services import auto_sell_view
from app.services import market_data_client
from app.services import trade_instructions as ti_svc


async def test_build_rows_marks_triggered_and_fired(monkeypatch):
    agent = uuid.uuid4()
    cust = SimpleNamespace(id=uuid.uuid4(), display_name="Azadi", agent_id=agent, broker="ayandeh")
    cust2 = SimpleNamespace(id=uuid.uuid4(), display_name="Bahar", agent_id=agent, broker="ayandeh")
    ti1 = SimpleNamespace(isin="IRO1A", auto_sell_threshold=500)   # queue 400 <= 500 → triggered
    ti2 = SimpleNamespace(isin="IRO1B", auto_sell_threshold=100)   # queue 900 > 100 → armed

    async def _armed(_db, _agent_id=None):
        return [(ti1, cust), (ti2, cust2)]

    monkeypatch.setattr(ti_svc, "list_armed_auto_sell", _armed)

    async def _queue(_db, isin):
        return {"buy_volume": 400 if isin == "IRO1A" else 900}

    monkeypatch.setattr(market_data_client, "get_queue", _queue)

    # order_fires query → cust fired IRO1A today.
    fired_res = MagicMock()
    fired_res.all = MagicMock(return_value=[(cust.id, "IRO1A")])
    db = MagicMock()
    db.execute = AsyncMock(return_value=fired_res)

    rows = await auto_sell_view.build_auto_sell_rows(db)
    by_isin = {r["isin"]: r for r in rows}

    assert by_isin["IRO1A"]["buy_volume"] == 400
    assert by_isin["IRO1A"]["triggered"] is True
    assert by_isin["IRO1A"]["fired_today"] is True
    assert by_isin["IRO1B"]["triggered"] is False     # 900 > 100
    assert by_isin["IRO1B"]["fired_today"] is False


async def test_build_rows_empty_when_none_armed(monkeypatch):
    async def _armed(_db, _agent_id=None):
        return []
    monkeypatch.setattr(ti_svc, "list_armed_auto_sell", _armed)
    db = MagicMock()
    assert await auto_sell_view.build_auto_sell_rows(db) == []


async def test_build_rows_applied_flag(monkeypatch):
    """The bot's last-applied threshold (from auto_sell_reload_status) → applied
    only when it equals the live DB threshold; a stale value reads as pending."""
    from datetime import datetime, timezone
    agent = uuid.uuid4()
    stack = uuid.uuid4()
    cust = SimpleNamespace(id=uuid.uuid4(), display_name="Az", agent_id=agent,
                           broker="ayandeh", stack_id=stack, username="acc1")
    cust2 = SimpleNamespace(id=uuid.uuid4(), display_name="Ba", agent_id=agent,
                            broker="ayandeh", stack_id=stack, username="acc2")
    ti1 = SimpleNamespace(isin="IRO1A", auto_sell_threshold=500)   # bot applied 500 → applied
    ti2 = SimpleNamespace(isin="IRO1B", auto_sell_threshold=300)   # bot still on 999 → pending

    async def _armed(_db, _agent_id=None):
        return [(ti1, cust), (ti2, cust2)]
    monkeypatch.setattr(ti_svc, "list_armed_auto_sell", _armed)

    async def _queue(_db, _isin):
        return {"buy_volume": 1000}
    monkeypatch.setattr(market_data_client, "get_queue", _queue)

    applied_at = datetime(2026, 6, 10, 11, 42, tzinfo=timezone.utc)
    fired_res = MagicMock(); fired_res.all = MagicMock(return_value=[])
    status_res = MagicMock(); status_res.all = MagicMock(return_value=[
        (stack, "acc1", "IRO1A", 500, applied_at),
        (stack, "acc2", "IRO1B", 999, applied_at),   # != live 300 → pending
    ])
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[fired_res, status_res])  # order_fires, then status

    rows = await auto_sell_view.build_auto_sell_rows(db)
    by = {r["isin"]: r for r in rows}
    assert by["IRO1A"]["applied"] is True
    assert by["IRO1A"]["applied_at"] == applied_at
    assert by["IRO1B"]["applied"] is False   # bot reload still pending
    assert by["IRO1B"]["applied_at"] == applied_at
