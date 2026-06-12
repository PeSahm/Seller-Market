"""Pure-function tests for ``app.services.broker_orders`` mapping + window.

The DB upsert path needs Postgres (``pg_insert``) so it's covered by the
end-to-end verification, not here. These tests pin the field mapping (a wrong
field name silently corrupts the fee report) and the market-open window logic.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal

from app.models.broker_orders import BrokerOrder
from app.models.customers import Customer
from app.services.broker_orders import in_time_window, map_getorders_row


# One real ephoenix GetOrders row (from the planning sample) — a شپنا buy
# placed in the market-open window.
_SAMPLE_ROW = {
    "id": 408420,
    "pamCode": "33094580090306",
    "isin": "IRO1PNES0001",
    "symbol": "شپنا",
    "symbolTitle": "شپنا",
    "date": "2026-06-01T08:45:01",
    "orderSide": 1,
    "volume": 200000,
    "price": 6310.0,
    "totalFee": 4684544.0,
    "executedAmount": 1266684544.0,
    "executedVolume": 200000,
    "executionDate": "2026-06-01T09:34:57",
    "trackingNumber": 909,
    "state": 3,
    "stateDesc": "کاملا انجام شده",
    "isDone": True,
    "created": "2026-06-01T08:45:00.657",
    "netTradedValue": 1262000000.0,
}


def _customer():
    return Customer(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        broker="ayandeh",
        username="4580090306",
        display_name="Mostafa main",
    )


def test_map_getorders_row_core_fields():
    cust = _customer()
    v = map_getorders_row(_SAMPLE_ROW, cust)
    assert v["customer_id"] == cust.id
    assert v["agent_id"] == cust.agent_id
    assert v["broker"] == "ayandeh"
    assert v["account_username"] == "4580090306"
    assert v["pam_code"] == "33094580090306"
    assert v["tracking_number"] == 909
    assert v["broker_order_id"] == 408420
    assert v["isin"] == "IRO1PNES0001"
    assert v["order_side"] == 1
    assert v["volume"] == 200000
    assert v["executed_volume"] == 200000
    assert v["state"] == 3
    assert v["is_done"] is True


def test_map_getorders_row_money_is_decimal():
    v = map_getorders_row(_SAMPLE_ROW, _customer())
    assert v["price"] == Decimal("6310.0")
    assert v["total_fee"] == Decimal("4684544.0")
    assert v["executed_amount"] == Decimal("1266684544.0")
    assert v["net_traded_value"] == Decimal("1262000000.0")
    # never floats
    assert isinstance(v["price"], Decimal)
    assert isinstance(v["executed_amount"], Decimal)


def test_map_getorders_row_parses_timestamps():
    v = map_getorders_row(_SAMPLE_ROW, _customer())
    assert v["placed_at"].year == 2026 and v["placed_at"].hour == 8
    assert v["created_at_broker"].second == 0 and v["created_at_broker"].microsecond == 657000
    assert v["execution_date"].hour == 9


def test_map_getorders_row_placed_date_from_date():
    # placed_date (dedup-key component, migration 0015) is the wall-clock DATE
    # of the placement timestamp.
    v = map_getorders_row(_SAMPLE_ROW, _customer())
    assert v["placed_date"] == date(2026, 6, 1)
    assert v["placed_date"] == v["placed_at"].date()


def test_map_getorders_row_placed_date_fallback_chain():
    # No "date" → falls back to "created"; no timestamps at all → sentinel
    # (a fixed value, NOT today — it must not change between re-fetches).
    row = dict(_SAMPLE_ROW)
    del row["date"]
    v = map_getorders_row(row, _customer())
    assert v["placed_at"] is None
    assert v["placed_date"] == date(2026, 6, 1)  # from "created"

    bare = dict(_SAMPLE_ROW)
    for k in ("date", "created", "executionDate"):
        del bare[k]
    v = map_getorders_row(bare, _customer())
    assert v["placed_date"] == date(1970, 1, 1)


def test_map_exir_row_placed_date_from_entry_datetime():
    # Exir rows derive placed_date from the (Jalali) entryDateTime.
    from app.services.broker_orders import _map_exir_row

    cust = _customer()
    row = {
        "mmtpOrderId": 12345,
        "insMaxLCode": "IRO1SROD0001",
        "orderSideName": "خريد",
        "entryDateTime": "1405/03/11-09:37:19",
        "quantity": 100,
        "tradedQuantity": 100,
        "price": 9930,
        "remainingQuantity": 0,
    }
    v = _map_exir_row(row, cust)
    assert v["placed_at"] is not None
    assert v["placed_date"] == v["placed_at"].date()


def test_in_time_window_matches_market_open_burst():
    o = BrokerOrder(
        created_at_broker=datetime(2026, 6, 1, 8, 44, 59, 580000, tzinfo=timezone.utc),
        placed_at=datetime(2026, 6, 1, 8, 45, 0, tzinfo=timezone.utc),
    )
    assert in_time_window(o, time(8, 44, 59), time(8, 45, 3)) is True


def test_in_time_window_excludes_outside():
    o = BrokerOrder(
        created_at_broker=datetime(2026, 6, 1, 14, 23, 34, tzinfo=timezone.utc),
        placed_at=datetime(2026, 6, 1, 14, 23, 34, tzinfo=timezone.utc),
    )
    assert in_time_window(o, time(8, 44, 59), time(8, 45, 3)) is False


def test_in_time_window_open_ended_when_no_bounds():
    o = BrokerOrder(
        created_at_broker=datetime(2026, 6, 1, 14, 23, 34, tzinfo=timezone.utc),
        placed_at=None,
    )
    assert in_time_window(o, None, None) is True
