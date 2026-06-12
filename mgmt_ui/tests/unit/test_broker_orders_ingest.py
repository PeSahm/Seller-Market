"""Mock-level tests for the ingest fetch loop + the collision repair.

The real upsert needs Postgres (``pg_insert`` + ON CONFLICT) and is covered by
end-to-end verification; here we pin the loop-level guards (foreign-account
rows are SKIPPED, not stored) and the shape of
:func:`app.services.broker_orders.repair_collision_rows`.
"""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.customers import Customer
from app.services import broker_orders as bo

_CID = uuid.uuid4()


def _customer():
    return Customer(
        id=_CID,
        agent_id=uuid.uuid4(),
        broker="karamad",
        username="0013419579",
        display_name="اعظم عالی",
        password_enc=b"x",
    )


def _row(tracking, pam):
    return {
        "trackingNumber": tracking,
        "pamCode": pam,
        "isin": "IRO1MSMI0001",
        "symbol": "فملی",
        "orderSide": 1,
        "price": 15080.0,
        "volume": 100,
        "executedVolume": 100,
        "state": 3,
        "isDone": True,
        "date": "2026-05-24T12:51:28",
    }


async def test_fetch_skips_foreign_account_rows(monkeypatch):
    cust = _customer()
    own = _row(1215, "330" + cust.username)  # pamCode ends with the username
    foreign = _row(984, "33090381127516")    # someone else's account

    async def _get_orders(**_kw):
        return [own, foreign], None
    monkeypatch.setattr(bo.broker_client, "get_orders", _get_orders)
    monkeypatch.setattr(bo.crypto, "decrypt", lambda _enc: "pw")

    upserted: list[dict] = []

    async def _upsert(_db, values):
        upserted.append(values)
        return True
    monkeypatch.setattr(bo, "_upsert_order", _upsert)

    res = await bo.fetch_and_upsert_orders(
        MagicMock(), cust,
        from_date=date(2026, 5, 1), to_date=date(2026, 6, 1),
        ocr_service_url="http://ocr",
    )
    # The foreign row is counted + skipped — never stored under this customer.
    assert res.pam_mismatches == 1
    assert res.inserted == 1
    assert [v["tracking_number"] for v in upserted] == [1215]


def _repair_db(contaminated_rows, customer_ids, fires_rowcount=3):
    """Fake AsyncSession for repair_collision_rows: execute() answers, in
    order: contaminated SELECT, customers SELECT, DELETE, fires UPDATE."""
    sel_contaminated = MagicMock()
    sel_contaminated.all.return_value = contaminated_rows
    sel_customers = MagicMock()
    sel_customers.all.return_value = [(cid,) for cid in customer_ids]
    del_res = MagicMock()
    upd_res = MagicMock()
    upd_res.rowcount = fires_rowcount

    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[sel_contaminated, sel_customers, del_res, upd_res]
    )
    db.commit = AsyncMock()
    return db


async def test_repair_dry_run_reports_without_writing():
    rows = [
        SimpleNamespace(id=uuid.uuid4(), broker="karamad"),
        SimpleNamespace(id=uuid.uuid4(), broker="karamad"),
        SimpleNamespace(id=uuid.uuid4(), broker="ayandeh"),
    ]
    db = _repair_db(rows, [uuid.uuid4()])
    summary = await bo.repair_collision_rows(db, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["contaminated_per_broker"] == {"karamad": 2, "ayandeh": 1}
    assert summary["affected_brokers"] == ["ayandeh", "karamad"]
    assert summary["deleted"] == 0 and summary["fires_reset"] == 0
    # Only the two SELECTs ran — no DELETE, no fires UPDATE, no commit.
    assert db.execute.await_count == 2
    db.commit.assert_not_awaited()


async def test_repair_deletes_and_resets_fires():
    rows = [SimpleNamespace(id=uuid.uuid4(), broker="karamad")]
    cids = [uuid.uuid4(), uuid.uuid4()]
    db = _repair_db(rows, cids, fires_rowcount=5)
    summary = await bo.repair_collision_rows(db)
    assert summary["deleted"] == 1
    assert summary["fires_reset"] == 5
    assert summary["affected_customer_ids"] == cids
    assert db.execute.await_count == 4  # select, select, delete, fires update
    db.commit.assert_awaited_once()


async def test_repair_clean_db_is_noop():
    db = MagicMock()
    sel = MagicMock()
    sel.all.return_value = []
    db.execute = AsyncMock(return_value=sel)
    db.commit = AsyncMock()
    summary = await bo.repair_collision_rows(db)
    assert summary["deleted"] == 0
    assert summary["contaminated_per_broker"] == {}
    assert db.execute.await_count == 1  # only the contaminated SELECT
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("dry", [True, False])
async def test_repair_summary_has_stable_keys(dry):
    db = MagicMock()
    sel = MagicMock()
    sel.all.return_value = []
    db.execute = AsyncMock(return_value=sel)
    db.commit = AsyncMock()
    summary = await bo.repair_collision_rows(db, dry_run=dry)
    assert set(summary) == {
        "dry_run", "contaminated_per_broker", "deleted", "fires_reset",
        "affected_brokers", "affected_customer_ids",
    }
