"""Unit tests for the Exir broker family: X-App-N token, Jalali conversion,
the orderbookReport row mapper, and registry family routing.

All pure/sync — no DB, no network. The live wire shape these assert against is
documented in ``SellerMarket/scratch/EXIR_FINDINGS.md`` (confirmed by the
Phase-0 spike against khobregan.exirbroker.com).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import app.services.brokers.exir as exir_mod
from app.services.brokers import registry
from app.services.brokers._jalali import (
    gregorian_str_to_jalali_str,
    gregorian_to_jalali,
    jalali_to_gregorian,
    parse_jalali_datetime,
)
from app.services.brokers.exir import ExirAdapter
from app.services.brokers.exir_token import build_app_n


# --------------------------------------------------------------------------
# X-App-N token
# --------------------------------------------------------------------------
def test_build_app_n_known_vector():
    """Offline-computed reference value pins the algorithm.

    nt="0034567890123" -> nt[0:2]="00"(=0), text="34567890123" (len 11).
    path="/a" -> char_sum = ord('/')+ord('a') = 47+97 = 144.
    now 00:00:10 UTC -> t = 10. idx = abs(10 % (11-5) - 0) = 4 -> text[4:9]="78901".
    int = 78901*10*144 = 113617440 ; frac = 10*144 = 1440.
    """
    nt = "0034567890123"
    now = datetime(2020, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
    assert build_app_n(nt, "/a", now) == "113617440.1440"


def test_build_app_n_shape_and_determinism():
    # Synthetic 130-digit nt (same length as a real one; not a real session seed).
    nt = "37" + "0123456789" * 12 + "01234567"
    now = datetime(2026, 6, 2, 10, 29, 45, tzinfo=timezone.utc)
    tok = build_app_n(nt, "/api/v2/user/buyingPower", now)
    # exactly one dot, both halves are integers
    int_part, frac_part = tok.split(".")
    assert int_part.lstrip("-").isdigit() and frac_part.isdigit()
    # deterministic for the same inputs
    assert build_app_n(nt, "/api/v2/user/buyingPower", now) == tok
    # path sensitivity: a different path changes the token
    assert build_app_n(nt, "/api/v1/order", now) != tok


def test_build_app_n_rejects_short_nt():
    with pytest.raises(ValueError):
        build_app_n("12345", "/a", datetime(2020, 1, 1, tzinfo=timezone.utc))


# --------------------------------------------------------------------------
# Jalali conversion
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "g,j",
    [
        ((2026, 6, 2), (1405, 3, 12)),   # spike date
        ((2024, 3, 20), (1403, 1, 1)),   # Nowruz 1403
        ((2021, 3, 21), (1400, 1, 1)),   # Nowruz 1400
    ],
)
def test_jalali_roundtrip(g, j):
    assert gregorian_to_jalali(*g) == j
    assert jalali_to_gregorian(*j) == g


def test_gregorian_str_to_jalali_str():
    assert gregorian_str_to_jalali_str("2026/06/02") == "1405/03/12"
    assert gregorian_str_to_jalali_str("") == ""


def test_parse_jalali_datetime():
    dt = parse_jalali_datetime("1405/03/12-13:27:08")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 6, 2)
    assert (dt.hour, dt.minute, dt.second) == (13, 27, 8)
    # tz-aware, Tehran +03:30
    assert dt.utcoffset().total_seconds() == 3.5 * 3600
    assert parse_jalali_datetime("garbage") is None
    assert parse_jalali_datetime("") is None


# --------------------------------------------------------------------------
# registry routing
# --------------------------------------------------------------------------
@pytest.fixture
def family_map():
    """Snapshot + restore the module family cache so we don't leak into others."""
    saved = registry._FAMILY_CACHE
    registry.set_family_map({"gs": "ephoenix", "khobregan": "exir"})
    try:
        yield
    finally:
        registry._FAMILY_CACHE = saved


def test_get_adapter_routes_by_family(family_map):
    from app.services.brokers.ephoenix import EphoenixAdapter

    a = registry.get_adapter("khobregan")
    assert isinstance(a, ExirAdapter) and a.family == "exir"
    assert a.code == "khobregan"

    e = registry.get_adapter("gs")
    assert isinstance(e, EphoenixAdapter) and e.family == "ephoenix"


def test_family_of_unknown_raises(family_map):
    with pytest.raises(registry.UnknownBrokerError):
        registry.family_of("does-not-exist")


# --------------------------------------------------------------------------
# Exir orderbookReport row mapping
# --------------------------------------------------------------------------
def _exir_row():
    """A realistic filled-buy row, fields per the live spike."""
    return {
        "mmtpOrderId": 17290408,
        "orderSideName": "خريد",  # buy (Arabic yeh)
        "quantity": 616,
        "remainingQuantity": 0,
        "tradedQuantity": 616,
        "price": 9690,
        "averageTradedPrice": 9690,
        "totalValue": 5969040,
        "pureValue": 5969040,
        "insMaxLCode": "IRO1SROD0001",
        "farsiName": "سيمان شاهرود",
        "mmtpOrderStatusName": "اجرا شده",
        "entryDateTime": "1405/03/12-13:27:08",
        "accountNumber": "11694580090306",
    }


def test_map_exir_row(family_map):
    from app.services.broker_orders import map_getorders_row

    customer = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        broker="khobregan",
        username="4580090306",
    )
    out = map_getorders_row(_exir_row(), customer)

    assert out["tracking_number"] == 17290408
    assert out["isin"] == "IRO1SROD0001"
    assert out["symbol"] == "IRO1SROD0001"
    assert out["symbol_title"] == "سيمان شاهرود"
    assert out["order_side"] == 1  # خريد -> buy
    assert out["volume"] == 616
    assert out["executed_volume"] == 616
    assert out["state"] == 3
    assert out["is_done"] is True
    assert out["serial_number"] is None
    assert out["total_fee"] is None
    assert out["pam_code"] == "11694580090306"
    assert out["placed_at"] is not None
    assert (out["placed_at"].year, out["placed_at"].month) == (2026, 6)
    # placed_at must be UTC-labeled wall-clock (matching the ephoenix mapper),
    # NOT +03:30, so the UTC-boundary date-range filters classify both families
    # consistently near Tehran midnight.
    assert out["placed_at"].utcoffset() == timedelta(0)
    assert (out["placed_at"].hour, out["placed_at"].minute) == (13, 27)


def test_map_exir_row_sell_side(family_map):
    from app.services.broker_orders import map_getorders_row

    customer = SimpleNamespace(id=uuid4(), agent_id=uuid4(), broker="khobregan", username="x")
    row = _exir_row()
    row["orderSideName"] = "فروش"  # sell
    out = map_getorders_row(row, customer)
    assert out["order_side"] == 2


def test_map_exir_and_ephoenix_same_keyset(family_map):
    """Both family mappers must populate the identical column set."""
    from app.services.broker_orders import _map_ephoenix_row, _map_exir_row

    customer = SimpleNamespace(id=uuid4(), agent_id=uuid4(), broker="khobregan", username="x")
    assert set(_map_exir_row(_exir_row(), customer).keys()) == set(
        _map_ephoenix_row({}, customer).keys()
    )


async def test_exir_get_orders_rejects_non_filled_status():
    """The adapter is canonical-filled-only: a request that excludes status 3
    returns an error WITHOUT touching the network (the check precedes any login),
    so the mapper's stamped state=3 can never mis-label a non-traded row."""
    rows, err = await ExirAdapter("khobregan").get_orders(
        "u",
        "p",
        "http://ocr.invalid",
        from_date="2026/06/01",
        to_date="2026/06/02",
        include_status=[2],
    )
    assert rows == []
    assert err is not None and "status 3" in err


# --------------------------------------------------------------------------
# get_orders: executed-only, no wire status filter
# --------------------------------------------------------------------------
class _FakeResp:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


async def test_exir_get_orders_keeps_only_traded_rows(monkeypatch):
    """The on-wire order status is unreliable (the spike's ``orderStatusId=3``
    returns nothing historically), so get_orders fetches WITHOUT a status filter
    and keeps only rows that actually traded (``tradedQuantity > 0``)."""
    captured: dict = {}

    async def fake_session(self, u, p, o):
        return {"nt": "3" + "0123456789" * 12 + "012345678", "cookies": {}}

    payload = {"result": [
        {"mmtpOrderId": 1, "insMaxLCode": "IRO1AAA00001",
         "tradedQuantity": 100, "orderSideName": "خريد"},
        {"mmtpOrderId": 2, "insMaxLCode": "IRO1BBB00001",
         "tradedQuantity": 0, "orderSideName": "خريد"},     # placed, never traded
        {"mmtpOrderId": 3, "insMaxLCode": "IRO1CCC00001",
         "orderSideName": "فروش"},                           # no traded qty at all
        {"mmtpOrderId": 4, "insMaxLCode": "IRO1DDD00001",
         "tradedQuantity": 50, "orderSideName": "فروش"},
    ]}

    async def fake_signed_get(self, client, nt, path):
        captured["path"] = path
        return _FakeResp(payload)

    monkeypatch.setattr(ExirAdapter, "_session", fake_session)
    monkeypatch.setattr(ExirAdapter, "_signed_get", fake_signed_get)

    rows, err = await ExirAdapter("khobregan").get_orders(
        "u", "p", "http://ocr",
        from_date="2026/06/01", to_date="2026/06/24", include_status=[3],
    )
    assert err is None
    # No wire status filter (the bug was hardcoding orderStatusId=3).
    assert "orderStatusId" not in captured["path"]
    # Only the rows that traded survive.
    assert sorted(r["mmtpOrderId"] for r in rows) == [1, 4]


async def test_exir_get_orders_isin_filter(monkeypatch):
    async def fake_session(self, u, p, o):
        return {"nt": "3" + "0123456789" * 12 + "012345678", "cookies": {}}

    payload = {"result": [
        {"mmtpOrderId": 1, "insMaxLCode": "IRO1AAA00001", "tradedQuantity": 100},
        {"mmtpOrderId": 4, "insMaxLCode": "IRO1DDD00001", "tradedQuantity": 50},
    ]}

    async def fake_signed_get(self, client, nt, path):
        return _FakeResp(payload)

    monkeypatch.setattr(ExirAdapter, "_session", fake_session)
    monkeypatch.setattr(ExirAdapter, "_signed_get", fake_signed_get)

    rows, err = await ExirAdapter("khobregan").get_orders(
        "u", "p", "http://ocr",
        from_date="2026/06/01", to_date="2026/06/24",
        include_status=[3], isin="IRO1DDD00001",
    )
    assert err is None
    assert [r["mmtpOrderId"] for r in rows] == [4]


# --------------------------------------------------------------------------
# _map_exir_row: realized fill price
# --------------------------------------------------------------------------
def test_map_exir_row_uses_average_traded_price(family_map):
    """Fee math wants the realized fill price (``averageTradedPrice``), not the
    limit ``price`` — a ceiling buy can fill below its limit."""
    from app.services.broker_orders import _map_exir_row

    customer = SimpleNamespace(id=uuid4(), agent_id=uuid4(), broker="khobregan", username="x")
    row = _exir_row()
    row["price"] = 9690        # limit price
    row["averageTradedPrice"] = 9650  # realized fill
    out = _map_exir_row(row, customer)
    assert out["price"] == Decimal("9650")


def test_map_exir_row_price_falls_back_to_order_price(family_map):
    from app.services.broker_orders import _map_exir_row

    customer = SimpleNamespace(id=uuid4(), agent_id=uuid4(), broker="khobregan", username="x")
    row = _exir_row()
    row["averageTradedPrice"] = None
    row["price"] = 9690
    out = _map_exir_row(row, customer)
    assert out["price"] == Decimal("9690")


# --------------------------------------------------------------------------
# verify_isin via the public RLC market-data backend
# --------------------------------------------------------------------------
async def test_exir_verify_isin_ok(monkeypatch):
    row = {
        "nc": "IRO1SROD0001", "sf": "سرود", "cn": "سيمان شاهرود",
        "hap": "9930.00", "lap": "9370.00", "ltp": "9700", "mxqo": "50000",
    }
    monkeypatch.setattr(exir_mod, "_rlc_instrument", AsyncMock(return_value=row))
    info = await ExirAdapter("khobregan").verify_isin("u", "p", "IRO1SROD0001", "http://ocr")
    assert info.ok is True
    assert info.isin == "IRO1SROD0001"
    assert info.symbol == "سرود"
    assert info.title == "سيمان شاهرود"
    assert info.max_price == 9930.0
    assert info.min_price == 9370.0
    assert info.last_price == 9700.0
    assert info.max_volume == 50000


async def test_exir_verify_isin_unknown(monkeypatch):
    monkeypatch.setattr(exir_mod, "_rlc_instrument", AsyncMock(return_value=None))
    info = await ExirAdapter("khobregan").verify_isin("u", "p", "IRO1XXXX0001", "http://ocr")
    assert info.ok is False
    # The verify partial renders ``.error`` on a failed lookup, not ``.message``.
    assert "not found" in (info.error or "").lower()


async def test_exir_verify_isin_unreachable(monkeypatch):
    monkeypatch.setattr(
        exir_mod, "_rlc_instrument", AsyncMock(side_effect=RuntimeError("boom"))
    )
    info = await ExirAdapter("khobregan").verify_isin("u", "p", "IRO1SROD0001", "http://ocr")
    assert info.ok is False
    assert "could not reach" in (info.error or "").lower()


async def test_exir_verify_isin_blank():
    info = await ExirAdapter("khobregan").verify_isin("u", "p", "   ", "http://ocr")
    assert info.ok is False
    assert (info.error or "").strip() != ""


def test_map_exir_row_rejects_nonfinite_price(family_map):
    """A malformed ('NaN'/'Infinity') traded price must NOT reach the money
    column — it falls back to the order price (and None if both are bad)."""
    from app.services.broker_orders import _map_exir_row

    customer = SimpleNamespace(id=uuid4(), agent_id=uuid4(), broker="khobregan", username="x")
    row = _exir_row()
    row["averageTradedPrice"] = "NaN"
    row["price"] = 9690
    assert _map_exir_row(row, customer)["price"] == Decimal("9690")

    row["price"] = "Infinity"
    assert _map_exir_row(row, customer)["price"] is None
