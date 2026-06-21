"""Unit tests for the open-positions aggregation (per customer × symbol) + the
agent ownership guard.

``build_open_positions`` reuses ``build_fee_report`` (mocked) and yields one row
per (customer, ISIN) with the open remainder. The agent route may only set/clear
a price for its OWN customer's position (404 otherwise).
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.broker_orders import BrokerOrder
from app.services import close_positions_view as cpv
from app.services.profit_report import BuyFeeRow, FeeReport

_AGENT = uuid.uuid4()
_CUST = uuid.uuid4()


def _buy_row(isin, price, open_vol, *, cust=_CUST, symbol="X"):
    o = BrokerOrder(
        customer_id=cust, agent_id=_AGENT, broker="ayandeh", account_username="u",
        tracking_number=1, isin=isin, symbol=symbol, order_side=1,
        price=Decimal(str(price)), volume=open_vol, executed_volume=open_vol,
        state=3, is_done=True, is_bot=True, raw_json={},
    )
    return BuyFeeRow(buy=o, open_volume=open_vol)


async def _setup(monkeypatch, report, *, last_price=None, saved=None):
    async def _setting(_db, key):
        from app.services.settings_store import DEFAULTS
        return DEFAULTS.get(key, "")
    monkeypatch.setattr(cpv.settings_store, "get_setting", _setting)

    async def _build(_db, **kw):
        return report
    monkeypatch.setattr(cpv, "build_fee_report", _build)

    async def _last(_db, _isin):
        return last_price
    monkeypatch.setattr(cpv.market_data_client, "get_last_price", _last)

    async def _close(_db, _pairs):
        return dict(saved or {})
    monkeypatch.setattr(cpv.close_prices_svc, "get_close_prices", _close)


async def test_one_row_per_customer_symbol(monkeypatch):
    cust_b = uuid.uuid4()
    report = FeeReport(buy_rows=[
        _buy_row("IRO1AAA", 6000, 100),                  # _CUST
        _buy_row("IRO1AAA", 6400, 100),                  # _CUST again, same symbol → blends
        _buy_row("IRO1AAA", 5000, 50, cust=cust_b),      # DIFFERENT customer, same symbol
        _buy_row("IRO1BBB", 5000, 50),                   # _CUST, other symbol
    ])
    await _setup(monkeypatch, report, last_price=7000,
                 saved={(_CUST, "IRO1AAA"): Decimal("7200")})
    rows = await cpv.build_open_positions(None, agent_id=_AGENT)
    by = {(r.customer_id, r.isin): r for r in rows}
    # _CUST's two IRO1AAA buys blend into ONE row
    assert by[(_CUST, "IRO1AAA")].open_qty == 200
    assert by[(_CUST, "IRO1AAA")].avg_buy_price == Decimal("6200")
    assert by[(_CUST, "IRO1AAA")].latest_price == 7000
    assert by[(_CUST, "IRO1AAA")].saved_price == Decimal("7200")
    # cust_b's IRO1AAA is a SEPARATE position, unaffected by _CUST's close
    assert by[(cust_b, "IRO1AAA")].open_qty == 50
    assert by[(cust_b, "IRO1AAA")].saved_price is None
    assert (_CUST, "IRO1BBB") in by
    assert len(rows) == 3


async def test_skips_fully_realized_and_unattributed(monkeypatch):
    report = FeeReport(buy_rows=[
        _buy_row("IRO1AAA", 6000, 0),                  # nothing open
        _buy_row("IRO1BBB", 6000, 100, cust=None),     # unattributed → skipped
    ])
    await _setup(monkeypatch, report)
    assert await cpv.build_open_positions(None, agent_id=_AGENT) == []


# --- agent ownership guard (404 on a foreign customer) ----------------------


def _user(role):
    return SimpleNamespace(role=role, id=uuid.uuid4())


async def test_agent_set_foreign_customer_404(monkeypatch):
    from app.routers import agent as agent_router
    user = _user("agent")

    async def _get_customer(_db, cid):
        return SimpleNamespace(id=cid, agent_id=uuid.uuid4())  # owned by SOMEONE ELSE
    monkeypatch.setattr(agent_router.services_customers, "get_customer", _get_customer)
    monkeypatch.setattr(agent_router, "_can_access_customer", lambda u, c: c.agent_id == u.id)

    called = {"set": 0}

    async def _set(*a, **k):
        called["set"] += 1
    monkeypatch.setattr(agent_router.services_close_prices, "set_close_price", _set)

    req = SimpleNamespace(headers={})
    with pytest.raises(HTTPException) as ei:
        await agent_router.agent_close_positions_set(
            req, user=user, db=None, customer_id=str(uuid.uuid4()),
            isin="IRO1AAA", price="7000", note="", next_url="")
    assert ei.value.status_code == 404
    assert called["set"] == 0


async def test_agent_set_own_customer_calls_service(monkeypatch):
    from app.routers import agent as agent_router
    user = _user("agent")
    cid = uuid.uuid4()

    async def _get_customer(_db, c):
        return SimpleNamespace(id=c, agent_id=user.id)  # owned by the agent
    monkeypatch.setattr(agent_router.services_customers, "get_customer", _get_customer)
    monkeypatch.setattr(agent_router, "_can_access_customer", lambda u, c: c.agent_id == u.id)

    calls = []

    async def _set(_db, customer_id, isin, price, note, actor_id, **k):
        calls.append((customer_id, isin, price))
    monkeypatch.setattr(agent_router.services_close_prices, "set_close_price", _set)

    req = SimpleNamespace(headers={})
    resp = await agent_router.agent_close_positions_set(
        req, user=user, db=None, customer_id=str(cid),
        isin="IRO1AAA", price="7200", note="", next_url="")
    assert calls == [(cid, "IRO1AAA", Decimal("7200"))]
    assert resp.status_code == 303  # non-HTMX → redirect


async def test_agent_set_rejects_non_finite_price(monkeypatch):
    from app.routers import agent as agent_router
    user = _user("agent")

    async def _get_customer(_db, c):  # pragma: no cover - 400 fires before this
        return SimpleNamespace(id=c, agent_id=user.id)
    monkeypatch.setattr(agent_router.services_customers, "get_customer", _get_customer)
    monkeypatch.setattr(agent_router, "_can_access_customer", lambda u, c: True)

    called = {"set": 0}

    async def _set(*a, **k):
        called["set"] += 1
    monkeypatch.setattr(agent_router.services_close_prices, "set_close_price", _set)

    req = SimpleNamespace(headers={})
    for bad in ("inf", "1e1000", "nan"):
        with pytest.raises(HTTPException) as ei:
            await agent_router.agent_close_positions_set(
                req, user=user, db=None, customer_id=str(uuid.uuid4()),
                isin="IRO1AAA", price=bad, note="", next_url="")
        assert ei.value.status_code == 400
    assert called["set"] == 0


# --- close-all bulk (skip already-priced, avg-buy fallback, one commit) ------


class _CommitDB:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _pos(cust, isin, saved, latest, avg):
    return SimpleNamespace(
        customer_id=cust, isin=isin, saved_price=saved, latest_price=latest,
        avg_buy_price=Decimal(str(avg)))


async def test_agent_close_all_skips_priced_and_falls_back(monkeypatch):
    from app.routers import agent as agent_router
    user = _user("agent")
    cA, cB, cC = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = [
        _pos(cA, "IRO1AAA", Decimal("7000"), 8000, 6000),  # already priced → SKIP
        _pos(cB, "IRO1BBB", None, 9930, 6000),             # latest price
        _pos(cC, "IRO1CCC", None, None, 5000),             # avg-buy fallback
    ]

    async def _open(_db, *, agent_id):
        return rows
    monkeypatch.setattr(agent_router.services_close_positions, "build_open_positions", _open)

    calls = []

    async def _set_if_absent(_db, customer_id, isin, price, note, actor_id, *, commit=True):
        calls.append((customer_id, isin, price, commit))
        return True
    monkeypatch.setattr(
        agent_router.services_close_prices, "set_close_price_if_absent", _set_if_absent)

    db = _CommitDB()
    req = SimpleNamespace(headers={})
    resp = await agent_router.agent_close_positions_close_all(req, user=user, db=db, next_url="")
    keys = [(c, i) for c, i, _p, _co in calls]
    assert (cA, "IRO1AAA") not in keys  # already priced — not clobbered
    assert (cB, "IRO1BBB", Decimal("9930"), False) in calls
    assert (cC, "IRO1CCC", Decimal("5000"), False) in calls
    assert db.commits == 1
    assert "closed=2" in resp.headers["Location"]
    assert "defaulted=1" in resp.headers["Location"]


async def test_admin_close_all_skips_priced_and_falls_back(monkeypatch):
    from app.routers import admin as admin_router
    user = _user("admin")
    cA, cB, cC = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = [
        _pos(cA, "IRO1AAA", Decimal("7000"), 8000, 6000),
        _pos(cB, "IRO1BBB", None, 9930, 6000),
        _pos(cC, "IRO1CCC", None, None, 5000),
    ]

    async def _open(_db, *, agent_id, broker=None):
        return rows
    monkeypatch.setattr(admin_router.services_close_positions, "build_open_positions", _open)

    calls = []

    async def _set_if_absent(_db, customer_id, isin, price, note, actor_id, *, commit=True):
        calls.append((customer_id, isin, price, commit))
        return True
    monkeypatch.setattr(
        admin_router.services_close_prices, "set_close_price_if_absent", _set_if_absent)

    db = _CommitDB()
    req = SimpleNamespace(headers={})
    resp = await admin_router.admin_close_positions_close_all(
        req, user=user, db=db, agent_id="", broker="", next_url="")
    keys = [(c, i) for c, i, _p, _co in calls]
    assert (cA, "IRO1AAA") not in keys
    assert (cB, "IRO1BBB", Decimal("9930"), False) in calls
    assert (cC, "IRO1CCC", Decimal("5000"), False) in calls
    assert db.commits == 1
    assert "closed=2" in resp.headers["Location"]
    assert "defaulted=1" in resp.headers["Location"]
