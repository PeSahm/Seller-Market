"""Unit tests for the Mofid / Orbis broker family (mgmt side): the login
classifier, the PKCE vector, the ``GET /core/api/order`` row mapper (the NUMERIC
side mapping is the highest-risk field), registry routing, verify_isin via the
shared RLC backend, and verify_credentials / get_orders with the OAuth login
mocked. All pure/sync or mocked — no DB, no network. Wire shapes confirmed by the
Phase-0 spike (account 4580090306) — see SellerMarket/scratch/MOFID_FINDINGS.md.
"""
from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import app.services.brokers._rlc as rlc_mod
import app.services.brokers.mofid as mofid
from app.services.brokers import registry
from app.services.brokers.base import CredStatus
from app.services.brokers.mofid import MofidAdapter, _MofidInvalidCredentials


# -------------------------------------------------------------- login classifier
def test_classify_invalid_credentials():
    html = '<div class="validation-summary-errors">نام کاربری یا کلمه عبور نادرست است</div>'
    assert mofid._classify_mofid_login(html) == "invalid_credentials"


def test_classify_captcha_markers():
    assert mofid._classify_mofid_login("کد امنیتی اشتباه است") == "wrong_captcha"
    assert mofid._classify_mofid_login("لطفا کد امنیتی را وارد کنید.") == "captcha_required"


def test_classify_conservative():
    # No recognised marker / non-str → None (never a false invalid).
    assert mofid._classify_mofid_login("something else") is None
    assert mofid._classify_mofid_login("") is None
    assert mofid._classify_mofid_login(None) is None
    assert mofid._classify_mofid_login({"x": 1}) is None
    # creds marker wins even if a captcha marker is also present.
    both = "کد امنیتی اشتباه است نام کاربری یا کلمه عبور نادرست است"
    assert mofid._classify_mofid_login(both) == "invalid_credentials"


def test_pkce_vector():
    v, c = mofid._pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    assert c == expected
    assert "=" not in c and len(v) >= 43  # RFC 7636 verifier length


# ----------------------------------------------------------------- row mapper
def _mofid_row(**over):
    row = {
        "id": 987654, "symbolIsin": "IRO1MSMI0001", "symbolName": "فملی",
        "price": 20930, "quantity": 100, "executedQuantity": 100,
        "side": 0, "createDateTime": "2026-06-30T08:45:01", "orderStateStr": "orderexecuted",
    }
    row.update(over)
    return row


def _customer():
    return SimpleNamespace(id=uuid4(), agent_id=uuid4(), broker="mofid", username="4580090306")


def test_map_mofid_side_buy_is_1():
    from app.services.broker_orders import _map_mofid_row
    out = _map_mofid_row(_mofid_row(side=0), _customer())
    assert out["order_side"] == 1  # numeric 0 = buy → 1


def test_map_mofid_side_sell_is_2():
    from app.services.broker_orders import _map_mofid_row
    out = _map_mofid_row(_mofid_row(side=1), _customer())
    assert out["order_side"] == 2  # numeric 1 = sell → 2


def test_map_mofid_side_unknown_is_0():
    from app.services.broker_orders import _map_mofid_row
    out = _map_mofid_row(_mofid_row(side=9), _customer())
    assert out["order_side"] == 0  # never default an unknown side to buy/sell


def test_map_mofid_fields():
    from app.services.broker_orders import _map_mofid_row
    out = _map_mofid_row(_mofid_row(), _customer())
    assert out["tracking_number"] == 987654
    assert out["broker_order_id"] == 987654
    assert out["serial_number"] is None  # → date-based reconcile
    assert out["isin"] == "IRO1MSMI0001"
    assert out["symbol"] == "فملی"
    assert out["executed_volume"] == 100 and out["volume"] == 100
    assert out["state"] == 3 and out["is_done"] is True


def test_map_mofid_same_keyset_as_ephoenix():
    from app.services.broker_orders import _map_ephoenix_row, _map_mofid_row
    cust = _customer()
    assert set(_map_mofid_row(_mofid_row(), cust).keys()) == set(
        _map_ephoenix_row({}, cust).keys()
    )


# ------------------------------------------------------------- registry routing
def test_registry_routes_to_mofid():
    registry.set_family_map({"mofid": "mofid"})
    adapter = registry.get_adapter("mofid")
    assert isinstance(adapter, MofidAdapter)
    assert adapter.family == "mofid"


# ----------------------------------------------------------------- verify_isin
async def test_verify_isin_via_rlc(monkeypatch):
    row = {"nc": "IRO1MSMI0001", "sf": "فملی", "cn": "ملی صنایع مس",
           "hap": "20930.00", "lap": "19730.00", "ltp": "20570", "mxqo": "100000"}
    monkeypatch.setattr(rlc_mod, "rlc_instrument", AsyncMock(return_value=row))
    info = await MofidAdapter("mofid").verify_isin("u", "p", "IRO1MSMI0001", "http://ocr")
    assert info.ok is True
    assert info.max_price == 20930.0 and info.min_price == 19730.0


# --- verify_credentials / get_orders (OAuth login mocked) ------------------
class _FakeReadCtx:
    """Fake async client context manager for the Bearer reads."""

    def __init__(self, status=200, payload=None):
        self._status = status
        self._payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return SimpleNamespace(status_code=self._status, json=lambda: self._payload, text="")


async def test_verify_credentials_ok(monkeypatch):
    monkeypatch.setattr(MofidAdapter, "_session", AsyncMock(return_value={"token": "JWT", "api": mofid._API_HOST}))
    monkeypatch.setattr(
        MofidAdapter, "_read_client",
        lambda self, s: _FakeReadCtx(200, {"name": "مصطفی", "family": "اسماعیلی", "bourseCode": "B123"}),
    )
    res = await MofidAdapter("mofid").verify_credentials("u", "p", "http://ocr")
    assert res.ok is True and res.status == CredStatus.VALID
    assert res.bourse_code == "B123"
    assert res.full_name == "مصطفی اسماعیلی"


async def test_verify_credentials_invalid(monkeypatch):
    monkeypatch.setattr(MofidAdapter, "_session", AsyncMock(side_effect=_MofidInvalidCredentials("rejected")))
    res = await MofidAdapter("mofid").verify_credentials("u", "p", "http://ocr")
    assert res.ok is False and res.status == CredStatus.INVALID_CREDENTIALS


async def test_verify_credentials_transient(monkeypatch):
    monkeypatch.setattr(MofidAdapter, "_session", AsyncMock(side_effect=RuntimeError("boom")))
    res = await MofidAdapter("mofid").verify_credentials("u", "p", "http://ocr")
    assert res.ok is False and res.status == CredStatus.TRANSIENT


async def test_get_orders_filters_unexecuted(monkeypatch):
    orders = {"orders": [
        {"id": 1, "symbolIsin": "A", "side": 0, "quantity": 10, "executedQuantity": 10,
         "price": 5, "createDateTime": "2026-06-30T08:45:00", "orderStateStr": "orderexecuted"},
        {"id": 2, "symbolIsin": "A", "side": 0, "quantity": 10, "executedQuantity": 0,
         "price": 5, "createDateTime": "2026-06-30T08:45:00", "orderStateStr": "onboard"},
    ]}
    monkeypatch.setattr(MofidAdapter, "_session", AsyncMock(return_value={"token": "JWT", "api": mofid._API_HOST}))
    monkeypatch.setattr(MofidAdapter, "_read_client", lambda self, s: _FakeReadCtx(200, orders))
    rows, warn = await MofidAdapter("mofid").get_orders(
        "u", "p", "http://ocr", from_date="2026/06/01", to_date="2026/06/30", include_status=[3],
    )
    assert [r["id"] for r in rows] == [1]  # only the executed row
    assert warn and "recent" in warn.lower()


async def test_get_orders_rejects_side_filter():
    rows, warn = await MofidAdapter("mofid").get_orders(
        "u", "p", "http://ocr", from_date="x", to_date="y", side=1,
    )
    assert rows == [] and "side" in (warn or "")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
