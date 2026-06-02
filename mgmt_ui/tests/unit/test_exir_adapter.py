"""Unit tests for the Exir broker family: X-App-N token, Jalali conversion,
the orderbookReport row mapper, and registry family routing.

All pure/sync — no DB, no network. The live wire shape these assert against is
documented in ``SellerMarket/scratch/EXIR_FINDINGS.md`` (confirmed by the
Phase-0 spike against khobregan.exirbroker.com).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

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
