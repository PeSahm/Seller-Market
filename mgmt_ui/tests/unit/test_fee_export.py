"""Test the Excel fee report (sell-side redesign, #111).

Builds a small :class:`FeeReport` (per-row sells + per-customer/agent totals)
from in-memory rows, renders the .xlsx, then loads it back with openpyxl to
assert the sheets + real numeric money cells.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO

from openpyxl import load_workbook

from app.models.broker_orders import BrokerOrder
from app.services.fee_export import build_fee_workbook
from app.services.profit_report import (
    AgentFeeTotals,
    CustomerFeeTotals,
    FeeReport,
    FeeRow,
    KIND_SELL,
)


def _sell_order(agent_id, customer_id):
    return BrokerOrder(
        customer_id=customer_id,
        agent_id=agent_id,
        broker="ayandeh",
        account_username="4580090306",
        tracking_number=1234,
        isin="IRO1PNES0001",
        symbol="شپنا",
        order_side=2,
        price=Decimal("6500"),
        volume=200000,
        executed_volume=200000,
        executed_amount=Decimal("1300000000"),
        total_fee=Decimal("4684544"),
        net_traded_value=Decimal("1295000000"),
        state=3,
        state_desc="کاملا انجام شده",
        is_done=True,
        placed_at=datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc),
        is_bot=True,
        raw_json={},
    )


def test_build_fee_workbook_sells_and_fees_sheet():
    agent_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    sell = _sell_order(agent_id, customer_id)
    row = FeeRow(
        customer_id=customer_id,
        agent_id=agent_id,
        broker="ayandeh",
        isin="IRO1PNES0001",
        symbol="شپنا",
        kind=KIND_SELL,
        qty=200000,
        price=Decimal("6500"),
        value=Decimal("1300000000"),
        fee_percent=Decimal("1.0"),
        fee=Decimal("13000000"),
        at=datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc),
        tracking=1234,
    )
    report = FeeReport(
        rows=[row],
        per_customer={customer_id: CustomerFeeTotals(
            customer_id=customer_id, agent_id=agent_id, num_sells=1,
            sell_fee=Decimal("13000000"), total_fee=Decimal("13000000"),
        )},
        per_agent={agent_id: AgentFeeTotals(
            agent_id=agent_id, num_rows=1,
            total_value=Decimal("1300000000"), total_fee=Decimal("13000000"),
        )},
        grand_value=Decimal("1300000000"),
        grand_fee=Decimal("13000000"),
    )

    data = build_fee_workbook(
        report, [sell],
        agent_names={agent_id: "mostafa"},
        customer_names={customer_id: "Mostafa main"},
    )
    assert isinstance(data, bytes) and len(data) > 0

    wb = load_workbook(BytesIO(data))
    assert wb.sheetnames == [
        "Sells & fees", "Per-customer totals", "Per-agent totals", "Raw orders",
    ]

    ws = wb["Sells & fees"]
    header = [c.value for c in ws[1]]
    assert {"Kind", "Value", "Fee amount"} <= set(header)
    data_row = {header[i]: ws[2][i].value for i in range(len(header))}
    assert data_row["Agent"] == "mostafa"
    assert data_row["Customer"] == "Mostafa main"
    assert data_row["ISIN"] == "IRO1PNES0001"
    assert data_row["Kind"] == KIND_SELL
    assert data_row["Fee amount"] == 13000000
    assert isinstance(data_row["Value"], (int, float))
    assert data_row["Value"] == 1300000000

    cust = wb["Per-customer totals"]
    assert "Total fee (owed)" in [c.value for c in cust[1]]
    agents = wb["Per-agent totals"]
    assert "Total fee" in [c.value for c in agents[1]]


def test_build_fee_workbook_empty_report_is_valid():
    data = build_fee_workbook(FeeReport(), [], agent_names={}, customer_names={})
    wb = load_workbook(BytesIO(data))
    assert "Sells & fees" in wb.sheetnames
    assert wb["Sells & fees"].max_row == 1  # header only
