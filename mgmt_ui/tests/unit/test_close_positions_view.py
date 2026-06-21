"""Unit tests for the open-positions aggregation + the agent ownership guard.

``build_open_positions`` reuses ``build_fee_report`` (mocked) and aggregates the
OPEN bot-buy remainder per ISIN. The agent route may only set/clear a price for
an instrument the agent holds open (404 otherwise).
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

    async def _close(_db, _isins):
        return dict(saved or {})
    monkeypatch.setattr(cpv.close_prices_svc, "get_close_prices", _close)


async def test_aggregates_open_volume_and_blended_avg_buy(monkeypatch):
    _CUST_B = uuid.uuid4()
    report = FeeReport(buy_rows=[
        _buy_row("IRO1AAA", 6000, 100),                 # _CUST
        _buy_row("IRO1AAA", 6400, 100, cust=_CUST_B),   # a SECOND customer, same ISIN
        _buy_row("IRO1BBB", 5000, 50),
    ])
    await _setup(monkeypatch, report, last_price=7000, saved={"IRO1AAA": Decimal("7200")})
    rows = await cpv.build_open_positions(None, agent_id=_AGENT)
    by = {r.isin: r for r in rows}
    assert by["IRO1AAA"].open_qty == 200
    assert by["IRO1AAA"].avg_buy_price == Decimal("6200")  # (100×6000 + 100×6400)/200
    assert by["IRO1AAA"].latest_price == 7000
    assert by["IRO1AAA"].saved_price == Decimal("7200")
    assert by["IRO1BBB"].saved_price is None
    assert by["IRO1AAA"].customer_count == 2  # two distinct customers on this ISIN
    assert by["IRO1BBB"].customer_count == 1


async def test_skips_fully_realized_rows(monkeypatch):
    report = FeeReport(buy_rows=[_buy_row("IRO1AAA", 6000, 0)])  # nothing open
    await _setup(monkeypatch, report)
    assert await cpv.build_open_positions(None, agent_id=_AGENT) == []


# --- agent ownership guard (404 on a foreign ISIN) --------------------------


async def test_agent_set_foreign_isin_404(monkeypatch):
    from app.routers import agent as agent_router

    user = SimpleNamespace(role="agent", id=uuid.uuid4())

    async def _open(_db, *, agent_id):
        return {"IRO1OTHER"}  # agent holds a DIFFERENT isin open
    monkeypatch.setattr(
        agent_router.services_close_positions, "list_open_isins", _open
    )

    called = {"set": 0}

    async def _set(*a, **k):
        called["set"] += 1
    monkeypatch.setattr(agent_router.services_close_prices, "set_close_price", _set)

    req = SimpleNamespace(headers={})
    with pytest.raises(HTTPException) as ei:
        await agent_router.agent_close_positions_set(
            req, user=user, db=None, isin="IRO1AAA", price="7000", note="", next_url=""
        )
    assert ei.value.status_code == 404
    assert called["set"] == 0


async def test_agent_set_owned_isin_calls_service(monkeypatch):
    from app.routers import agent as agent_router

    user = SimpleNamespace(role="agent", id=uuid.uuid4())

    async def _open(_db, *, agent_id):
        return {"IRO1AAA"}
    monkeypatch.setattr(
        agent_router.services_close_positions, "list_open_isins", _open
    )

    called = {"set": 0, "isin": None, "price": None}

    async def _set(_db, isin, price, note, actor_id, **k):
        called["set"] += 1
        called["isin"] = isin
        called["price"] = price
    monkeypatch.setattr(agent_router.services_close_prices, "set_close_price", _set)

    req = SimpleNamespace(headers={})
    resp = await agent_router.agent_close_positions_set(
        req, user=user, db=None, isin="IRO1AAA", price="7200", note="", next_url=""
    )
    assert called["set"] == 1
    assert called["isin"] == "IRO1AAA" and called["price"] == Decimal("7200")
    assert resp.status_code == 303  # non-HTMX → redirect


async def test_admin_set_bypasses_open_set(monkeypatch):
    # An admin may price any ISIN — _agent_open_isins returns None (no limit), so
    # list_open_isins must NOT even be consulted on the admin path.
    from app.routers import agent as agent_router

    user = SimpleNamespace(role="admin", id=uuid.uuid4())

    async def _open(_db, *, agent_id):  # pragma: no cover - must not run
        raise AssertionError("admin must bypass the open-set guard")
    monkeypatch.setattr(
        agent_router.services_close_positions, "list_open_isins", _open
    )

    called = {"set": 0}

    async def _set(*a, **k):
        called["set"] += 1
    monkeypatch.setattr(agent_router.services_close_prices, "set_close_price", _set)

    req = SimpleNamespace(headers={})
    await agent_router.agent_close_positions_set(
        req, user=user, db=None, isin="IRO1ZZZ", price="9000", note="", next_url=""
    )
    assert called["set"] == 1


async def test_agent_set_rejects_non_finite_price(monkeypatch):
    from app.routers import agent as agent_router

    user = SimpleNamespace(role="agent", id=uuid.uuid4())

    async def _open(_db, *, agent_id):  # pragma: no cover - guard runs after parse
        return {"IRO1AAA"}
    monkeypatch.setattr(
        agent_router.services_close_positions, "list_open_isins", _open
    )

    called = {"set": 0}

    async def _set(*a, **k):
        called["set"] += 1
    monkeypatch.setattr(agent_router.services_close_prices, "set_close_price", _set)

    req = SimpleNamespace(headers={})
    for bad in ("inf", "1e1000", "nan"):
        with pytest.raises(HTTPException) as ei:
            await agent_router.agent_close_positions_set(
                req, user=user, db=None, isin="IRO1AAA", price=bad, note="", next_url=""
            )
        assert ei.value.status_code == 400
    assert called["set"] == 0


# --- close-all bulk (skip already-priced, avg-buy fallback, one commit) ------


class _CommitDB:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _pos(isin, saved, latest, avg):
    return SimpleNamespace(
        isin=isin, saved_price=saved, latest_price=latest, avg_buy_price=Decimal(str(avg))
    )


async def test_agent_close_all_skips_priced_and_falls_back(monkeypatch):
    from app.routers import agent as agent_router

    user = SimpleNamespace(role="agent", id=uuid.uuid4())
    rows = [
        _pos("IRO1AAA", Decimal("7000"), 8000, 6000),  # already priced → SKIP
        _pos("IRO1BBB", None, 9930, 6000),             # latest price
        _pos("IRO1CCC", None, None, 5000),             # no market price → avg-buy fallback
    ]

    async def _open(_db, *, agent_id):
        return rows
    monkeypatch.setattr(
        agent_router.services_close_positions, "build_open_positions", _open
    )

    calls = []

    async def _set_if_absent(_db, isin, price, note, actor_id, *, commit=True):
        calls.append((isin, price, commit))
        return True  # inserted (no concurrent price)
    monkeypatch.setattr(
        agent_router.services_close_prices, "set_close_price_if_absent", _set_if_absent
    )

    db = _CommitDB()
    req = SimpleNamespace(headers={})
    resp = await agent_router.agent_close_positions_close_all(
        req, user=user, db=db, next_url=""
    )
    isins = [c[0] for c in calls]
    assert "IRO1AAA" not in isins  # already priced — not clobbered
    assert ("IRO1BBB", Decimal("9930"), False) in calls
    assert ("IRO1CCC", Decimal("5000"), False) in calls  # avg-buy fallback
    assert db.commits == 1  # single commit after staging both
    assert "closed=2" in resp.headers["Location"]
    assert "defaulted=1" in resp.headers["Location"]


async def test_admin_close_all_skips_priced_and_falls_back(monkeypatch):
    from app.routers import admin as admin_router

    user = SimpleNamespace(role="admin", id=uuid.uuid4())
    rows = [
        _pos("IRO1AAA", Decimal("7000"), 8000, 6000),  # already priced → SKIP
        _pos("IRO1BBB", None, 9930, 6000),             # latest price
        _pos("IRO1CCC", None, None, 5000),             # avg-buy fallback
    ]

    async def _open(_db, *, agent_id, broker=None):
        return rows
    monkeypatch.setattr(
        admin_router.services_close_positions, "build_open_positions", _open
    )

    calls = []

    async def _set_if_absent(_db, isin, price, note, actor_id, *, commit=True):
        calls.append((isin, price, commit))
        return True
    monkeypatch.setattr(
        admin_router.services_close_prices, "set_close_price_if_absent", _set_if_absent
    )

    db = _CommitDB()
    req = SimpleNamespace(headers={})
    resp = await admin_router.admin_close_positions_close_all(
        req, user=user, db=db, agent_id="", broker="", next_url=""
    )
    isins = [c[0] for c in calls]
    assert "IRO1AAA" not in isins
    assert ("IRO1BBB", Decimal("9930"), False) in calls
    assert ("IRO1CCC", Decimal("5000"), False) in calls
    assert db.commits == 1
    assert "closed=2" in resp.headers["Location"]
    assert "defaulted=1" in resp.headers["Location"]
