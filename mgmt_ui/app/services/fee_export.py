"""Render the bot fee report to an Excel (.xlsx) workbook.

This is the owner's primary deliverable: one row per successful bot BUY with
its matched/possible SELL and the realized fee, plus per-agent totals and a
raw-orders audit sheet. Money cells are real Excel numbers (not strings) so
the operator can sort/sum/pivot; dates are Excel dates.

Built with :mod:`openpyxl`. Returns raw ``bytes`` the route streams back.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from io import BytesIO
from typing import Optional
from uuid import UUID

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from app.models.broker_orders import BrokerOrder
from app.services.profit_report import FeeReport

_MONEY_FMT = "#,##0"
_DATE_FMT = "yyyy-mm-dd hh:mm:ss"
_PCT_FMT = "0.0000"
_HEADER_FILL = PatternFill("solid", fgColor="1F2937")
_HEADER_FONT = Font(bold=True, color="FFFFFF")

_SIDE_LABEL = {1: "Buy", 2: "Sell"}


def _num(value: Optional[Decimal]) -> Optional[float]:
    """Decimal → float for an Excel numeric cell. Rial magnitudes (~1e9–1e10)
    are well inside float64's exact-integer range, so no precision loss."""
    if value is None:
        return None
    return float(value)


def _xl_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """openpyxl rejects tz-aware datetimes — drop tzinfo (the stored value is
    already the broker's wall clock)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None)


def _write_header(ws, headers: list[str]) -> None:
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    ws.freeze_panes = "A2"


def _autosize(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def build_fee_workbook(
    report: FeeReport,
    orders: list[BrokerOrder],
    *,
    agent_names: dict[UUID, str],
    customer_names: dict[UUID, str],
    generated_for: str = "",
) -> bytes:
    """Render ``report`` (+ the raw ``orders``) into an .xlsx workbook.

    Sheets: per-row Sells & fees · Per-customer totals (owed) · Per-agent
    totals · Raw orders (audit). Each fee row is a real bot **sell** or a 20-day
    **virtual** sell (mark-to-market on an unsold bot buy).
    """
    wb = Workbook()

    # ---- Sheet 1: Sells & fees ------------------------------------------
    ws = wb.active
    ws.title = "Sells & fees"
    headers = [
        "Agent", "Customer", "Broker", "ISIN", "Symbol", "Kind",
        "Date", "Qty", "Price", "Value", "Fee %", "Fee amount", "Age (days)",
    ]
    _write_header(ws, headers)
    for row in report.rows:
        ws.append([
            agent_names.get(row.agent_id, "—") if row.agent_id else "—",
            customer_names.get(row.customer_id, "—") if row.customer_id else "—",
            row.broker,
            row.isin,
            row.symbol or "",
            row.kind,
            _xl_dt(row.at),
            int(row.qty),
            _num(row.price),
            _num(row.value),
            _num(row.fee_percent),
            _num(row.fee),
            row.age_days if row.age_days is not None else "",
        ])
    _apply_formats(
        ws,
        date_cols=[7],
        money_cols=[9, 10, 12],
        pct_cols=[11],
        nrows=len(report.rows),
    )
    _autosize(ws, [16, 18, 10, 16, 12, 9, 19, 12, 12, 18, 8, 16, 10])

    # ---- Sheet 2: Per-customer totals (owed) ----------------------------
    wsc = wb.create_sheet("Per-customer totals")
    _write_header(
        wsc,
        ["Customer", "Agent", "# sells", "# virtual", "Sell fee",
         "Virtual fee", "Total fee (owed)", "Paid", "Remaining"],
    )
    for t in report.per_customer.values():
        wsc.append([
            customer_names.get(t.customer_id, "—") if t.customer_id else "—",
            agent_names.get(t.agent_id, "—") if t.agent_id else "—",
            t.num_sells,
            t.num_virtual,
            _num(t.sell_fee),
            _num(t.virtual_fee),
            _num(t.total_fee),
            _num(t.paid),
            _num(t.remaining),
        ])
    _apply_formats(wsc, date_cols=[], money_cols=[5, 6, 7, 8, 9], pct_cols=[], nrows=len(report.per_customer))
    _autosize(wsc, [18, 16, 10, 10, 16, 16, 18, 16, 16])

    # ---- Sheet 3: Per-agent totals --------------------------------------
    ws2 = wb.create_sheet("Per-agent totals")
    _write_header(ws2, ["Agent", "# fee rows", "Total value", "Total fee"])
    for totals in report.per_agent.values():
        ws2.append([
            agent_names.get(totals.agent_id, "—") if totals.agent_id else "—",
            totals.num_rows,
            _num(totals.total_value),
            _num(totals.total_fee),
        ])
    ws2.append([])
    ws2.append(["TOTAL", "", _num(report.grand_value), _num(report.grand_fee)])
    _apply_formats(ws2, date_cols=[], money_cols=[3, 4], pct_cols=[], nrows=len(report.per_agent) + 2)
    _autosize(ws2, [18, 12, 18, 18])

    # ---- Sheet 3: Raw orders (audit) ------------------------------------
    ws3 = wb.create_sheet("Raw orders")
    _write_header(
        ws3,
        ["Tracking #", "Serial #", "Agent", "Customer", "Broker", "ISIN", "Symbol",
         "Side", "State", "Placed at", "Executed vol", "Price",
         "Executed amount", "Total fee", "Net traded value", "Bot?"],
    )
    for o in orders:
        ws3.append([
            o.tracking_number,
            getattr(o, "serial_number", None),
            agent_names.get(o.agent_id, "—") if o.agent_id else "—",
            customer_names.get(o.customer_id, o.account_username) if o.customer_id else o.account_username,
            o.broker,
            o.isin,
            o.symbol or "",
            _SIDE_LABEL.get(o.order_side, str(o.order_side)),
            o.state_desc or str(o.state),
            _xl_dt(o.placed_at or o.created_at_broker),
            int(o.executed_volume or 0),
            _num(o.price),
            _num(o.executed_amount),
            _num(o.total_fee),
            _num(o.net_traded_value),
            "yes" if o.is_bot else "",
        ])
    _apply_formats(ws3, date_cols=[10], money_cols=[12, 13, 14, 15], pct_cols=[], nrows=len(orders))
    _autosize(ws3, [14, 18, 16, 16, 10, 16, 10, 6, 18, 19, 12, 12, 16, 14, 16, 6])

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _apply_formats(ws, *, date_cols, money_cols, pct_cols, nrows) -> None:
    """Apply number/date formats to data rows (row 2 .. nrows+1)."""
    for r in range(2, nrows + 2):
        for c in date_cols:
            ws.cell(row=r, column=c).number_format = _DATE_FMT
        for c in money_cols:
            ws.cell(row=r, column=c).number_format = _MONEY_FMT
        for c in pct_cols:
            ws.cell(row=r, column=c).number_format = _PCT_FMT


__all__ = ["build_fee_workbook"]
