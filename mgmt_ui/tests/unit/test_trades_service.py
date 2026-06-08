"""can_user_see_trade permission tests + list_trades executed-only filter."""
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


# ---------------------------------------------------------------------------
# list_trades(executed_only=...) — the #107 fix. We capture the SQLAlchemy
# statement (no DB) and assert the executed_volume predicate is added ONLY
# when executed_only is True. The SELECT column list always names
# ``executed_volume`` (it's a column), so we match the comparison
# ``executed_volume >`` which appears solely in the WHERE clause.
# ---------------------------------------------------------------------------

class _FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _CapturingSession:
    def __init__(self):
        self.last_stmt = None

    async def execute(self, stmt):
        self.last_stmt = stmt
        return _FakeResult()


async def test_list_trades_executed_only_adds_filter():
    from app.services.trades import list_trades
    db = _CapturingSession()
    await list_trades(db, executed_only=True)
    assert "executed_volume >" in str(db.last_stmt)


async def test_list_trades_default_has_no_execution_filter():
    from app.services.trades import list_trades
    db = _CapturingSession()
    await list_trades(db)  # executed_only defaults to False
    assert "executed_volume >" not in str(db.last_stmt)
