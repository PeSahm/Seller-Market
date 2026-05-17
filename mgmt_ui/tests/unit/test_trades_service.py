"""can_user_see_trade permission tests."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4


def test_admin_can_see_any_trade():
    from app.services.trades import can_user_see_trade
    admin = SimpleNamespace(role="admin", id=uuid4())
    trade = SimpleNamespace(customer_id=uuid4())
    cust = SimpleNamespace(agent_id=uuid4())
    assert can_user_see_trade(admin, trade, cust) is True


def test_agent_sees_own_customer_trade():
    from app.services.trades import can_user_see_trade
    agent_id = uuid4()
    agent = SimpleNamespace(role="agent", id=agent_id)
    trade = SimpleNamespace(customer_id=uuid4())
    cust = SimpleNamespace(agent_id=agent_id)
    assert can_user_see_trade(agent, trade, cust) is True


def test_agent_blocked_on_other_agents_trade():
    from app.services.trades import can_user_see_trade
    me = SimpleNamespace(role="agent", id=uuid4())
    them = SimpleNamespace(agent_id=uuid4())  # different agent
    trade = SimpleNamespace(customer_id=uuid4())
    assert can_user_see_trade(me, trade, them) is False
