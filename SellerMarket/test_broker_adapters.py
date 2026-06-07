"""Hermetic tests for the bot-side broker-adapter layer (Phase 2, Exir feature).

NO real network, NO real broker. ``requests`` and the captcha decoder are
monkeypatched / faked; ``EphoenixAPIClient`` is faked. Runnable two ways:

    python -m pytest test_broker_adapters.py -q
    python test_broker_adapters.py        # falls back to a plain assert runner
"""
from __future__ import annotations

import base64
import json

import broker_adapters
import ephoenix_adapter
import exir_adapter
from broker_adapters import PreparedOrder, resolve_family, get_adapter


# ---------------------------------------------------------------------------
# resolve_family
# ---------------------------------------------------------------------------

def test_resolve_family_honors_config_broker_family():
    assert resolve_family("gs", {"broker_family": "exir"}) == "exir"
    # case/whitespace tolerant
    assert resolve_family("gs", {"broker_family": "  Exir "}) == "exir"
    assert resolve_family("ayandeh", {"broker_family": "ephoenix"}) == "ephoenix"


def test_resolve_family_falls_back_to_ephoenix():
    assert resolve_family("gs", {}) == "ephoenix"
    assert resolve_family("ayandeh", None) == "ephoenix"
    # empty/blank broker_family is treated as absent (factory still defaults)
    assert resolve_family("gs", {"broker_family": ""}) == "ephoenix"


# ---------------------------------------------------------------------------
# Exir login/network fakes
# ---------------------------------------------------------------------------

# A 130-char numeric `nt` seed (len-2 segment > 5 so the X-App-N algo is happy).
_FAKE_NT = "42" + ("1234567890" * 13)  # len == 132

# authToken JWT carrying {"b": 116}; only the payload segment matters here.
def _make_auth_token(broker_id: int) -> str:
    def seg(d):
        raw = json.dumps(d).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    header = seg({"alg": "none", "typ": "JWT"})
    payload = seg({"b": broker_id, "sub": "x"})
    return f"{header}.{payload}.sig"


class _FakeResp:
    def __init__(self, *, status=200, json_data=None, content=b"", headers=None):
        self.status_code = status
        self._json = json_data
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeCookieJar(dict):
    def set(self, k, v):
        self[k] = v


class _FakeSession:
    """Minimal stand-in for requests.Session used by ExirAdapter._login."""

    def __init__(self):
        self.cookies = _FakeCookieJar()
        self.headers = {}

    def get(self, url, timeout=None, **kw):
        if url.endswith("/exir"):
            self.cookies["cookiesession1"] = "boot"
            return _FakeResp(status=200)
        if url.endswith("/captcha"):
            # tiny JPEG bytes; OCR is faked to return digits regardless
            return _FakeResp(status=200, content=b"\xff\xd8jpeg",
                             headers={"client_login_id": "jwtish"})
        raise AssertionError(f"unexpected session.get({url})")

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        assert url.endswith("/api/v2/login")
        # captcha must be an int (JSON number), per the live wire shape
        assert isinstance(json["captcha"], int)
        self.cookies["JWT-TOKEN"] = "auth"
        return _FakeResp(status=200, json_data={
            "nt": _FAKE_NT,
            "authToken": _make_auth_token(116),
            "validity": 480,
            "username": "1164580090306",
        })


def _install_exir_fakes(monkeypatch, *, asset="1000000", holdings_rows=None, band=(200, 180), buy_fee=0.0, max_qty=0):
    """Patch ExirAdapter so login + signed reads + the RLC band never hit the network."""
    # Fresh module session cache each test.
    monkeypatch.setattr(exir_adapter, "_SESSION_CACHE", {}, raising=True)

    # Captcha decoder → always a valid 5-digit string.
    monkeypatch.setattr(exir_adapter, "_default_decode_captcha", lambda b64: "78529")

    # Session factory → our fake.
    monkeypatch.setattr(exir_adapter.requests, "Session", lambda: _FakeSession())

    # Broker-native RLC price band (ceiling, floor) — never hit the network.
    monkeypatch.setattr(
        exir_adapter.rlc_price, "get_price_band", lambda isin, timeout=15: band
    )
    # Max order quantity (RLC mxqo). 0 = "no cap" (default) so existing volume
    # assertions are unaffected; a positive value exercises the volume cap.
    monkeypatch.setattr(
        exir_adapter.rlc_price, "get_max_order_qty", lambda isin, timeout=15: max_qty
    )

    # Signed GET reads: buying power via stockInfo.purchaseUpperBound, holdings.
    def fake_get(url, headers=None, cookies=None, timeout=None, **kw):
        assert "X-App-N" in headers
        if url.endswith("/api/v1/user/stockInfo"):
            return _FakeResp(status=200, json_data={"purchaseUpperBound": asset})
        if "/api/v2/wages/instrument/" in url:
            isin = url.rsplit("/", 1)[-1]
            return _FakeResp(status=200, json_data={isin: {"SIDE_BUY": buy_fee, "SIDE_SALE": 0.0088}})
        if url.endswith("/api/v1/user/portfoReport"):
            return _FakeResp(status=200, json_data={"result": holdings_rows or []})
        raise AssertionError(f"unexpected requests.get({url})")

    monkeypatch.setattr(exir_adapter.requests, "get", fake_get)


# ---------------------------------------------------------------------------
# ExirAdapter — BUY
# ---------------------------------------------------------------------------

def test_exir_prepare_buy_body_and_signer(monkeypatch):
    _install_exir_fakes(monkeypatch, asset="1000000")
    a = exir_adapter.ExirAdapter("khobregan", "1164580090306", "pw")
    po = a.prepare_order(isin="IRO3SMBZ0001", side=1, config_section={"price": "200"})

    assert isinstance(po, PreparedOrder)
    body = json.loads(po.body)
    assert body["side"] == "SIDE_BUY"
    assert body["insMaxLcode"] == "IRO3SMBZ0001"
    assert body["orderType"] == "ORDER_TYPE_LIMIT"
    assert body["bankAccountId"] == -1
    assert body["brokerCode"] == 116           # decoded from authToken "b" claim
    assert body["coreType"] == "c"
    assert body["validityType"] == "VALIDITY_TYPE_DAY"
    # price/quantity are STRINGS
    assert body["price"] == "200.0" and isinstance(body["price"], str)
    assert isinstance(body["quantity"], str)
    # volume == asset // price == 1000000 // 200 == 5000
    assert po.volume == 5000
    assert body["quantity"] == "5000"
    # exir auth: no bearer, signer + cookies present
    assert po.bearer_token is None
    assert callable(po.signer)
    assert po.cookies and "JWT-TOKEN" in po.cookies
    assert po.order_url == "https://khobregan.exirbroker.com/api/v1/order"
    # signer yields a {"X-App-N": "<digits>.<digits>"} dict
    sig = po.signer()
    assert set(sig.keys()) == {"X-App-N"}
    left, _, right = sig["X-App-N"].partition(".")
    assert left.isdigit() and right.isdigit() and right != ""


def test_exir_prepare_buy_respects_max_volume(monkeypatch):
    _install_exir_fakes(monkeypatch, asset="1000000")
    a = exir_adapter.ExirAdapter("khobregan", "1164580090306", "pw")
    # asset//price would be 5000, cap to 1234
    po = a.prepare_order(isin="IRO3SMBZ0001", side=1,
                         config_section={"price": "200", "max_volume": 1234})
    assert po.volume == 1234
    assert json.loads(po.body)["quantity"] == "1234"


# ---------------------------------------------------------------------------
# ExirAdapter — SELL
# ---------------------------------------------------------------------------

def test_exir_prepare_sell_uses_holdings(monkeypatch):
    rows = [{"insMaxLcode": "IRO3SMBZ0001", "asset": 777}]
    _install_exir_fakes(monkeypatch, holdings_rows=rows)
    a = exir_adapter.ExirAdapter("khobregan", "1164580090306", "pw")
    po = a.prepare_order(isin="IRO3SMBZ0001", side=2, config_section={"price": "200"})
    body = json.loads(po.body)
    assert body["side"] == "SIDE_SALE"
    assert po.volume == 777
    assert body["quantity"] == "777"


def test_exir_sell_no_holdings_raises(monkeypatch):
    _install_exir_fakes(monkeypatch, holdings_rows=[])
    a = exir_adapter.ExirAdapter("khobregan", "1164580090306", "pw")
    try:
        a.prepare_order(isin="IRO3SMBZ0001", side=2, config_section={"price": "200"})
        raise AssertionError("expected ValueError for no holdings")
    except ValueError as e:
        assert "no Exir holdings" in str(e)


# ---------------------------------------------------------------------------
# ExirAdapter — price band from the broker's RLC gateway (no config price)
# ---------------------------------------------------------------------------

def test_exir_buy_uses_rlc_ceiling(monkeypatch):
    # band ceiling=9930 (BUY price), asset 993000 → volume 993000//9930 == 100.
    _install_exir_fakes(monkeypatch, asset="993000", band=(9930, 9370))
    a = exir_adapter.ExirAdapter("khobregan", "1164580090306", "pw")
    po = a.prepare_order(isin="IRO1SROD0001", side=1, config_section={})  # NO config price
    body = json.loads(po.body)
    assert body["price"] == "9930.0"       # BUY → RLC ceiling
    assert po.volume == 100
    assert body["quantity"] == "100"


def test_exir_buy_volume_is_fee_adjusted(monkeypatch):
    # BP=6,000,000, ceiling=9930, buy fee 0.3712% → floor(6e6/(9930*1.003712)) == 601
    # (the naive 6e6//9930 == 604 would over-spend the buying power and be rejected;
    # 602 already overshoots by ~49 Rials, so the correct floor is 601 — matches live).
    _install_exir_fakes(monkeypatch, asset="6000000", band=(9930, 9370), buy_fee=0.003712)
    a = exir_adapter.ExirAdapter("khobregan", "1164580090306", "pw")
    po = a.prepare_order(isin="IRO1SROD0001", side=1, config_section={})
    assert po.volume == int(6000000 / (9930 * 1.003712))   # == 601
    assert po.volume == 601


def test_exir_sell_uses_rlc_floor(monkeypatch):
    rows = [{"insMaxLcode": "IRO1SROD0001", "asset": 50}]
    _install_exir_fakes(monkeypatch, holdings_rows=rows, band=(9930, 9370))
    a = exir_adapter.ExirAdapter("khobregan", "1164580090306", "pw")
    po = a.prepare_order(isin="IRO1SROD0001", side=2, config_section={})  # NO config price
    body = json.loads(po.body)
    assert body["price"] == "9370.0"       # SELL → RLC floor
    assert po.volume == 50


def test_exir_no_band_raises(monkeypatch):
    _install_exir_fakes(monkeypatch)
    monkeypatch.setattr(
        exir_adapter.rlc_price, "get_price_band",
        lambda isin, timeout=15: (_ for _ in ()).throw(ValueError("no rlc price band")),
    )
    a = exir_adapter.ExirAdapter("khobregan", "1164580090306", "pw")
    try:
        a.prepare_order(isin="ZZZ", side=1, config_section={})
        raise AssertionError("expected ValueError when RLC has no band")
    except ValueError as e:
        assert "rlc price band" in str(e)


def test_exir_config_price_overrides_rlc(monkeypatch):
    # An explicit config price is honoured as an override (band ignored).
    _install_exir_fakes(monkeypatch, asset="1000000", band=(9930, 9370))
    a = exir_adapter.ExirAdapter("khobregan", "1164580090306", "pw")
    po = a.prepare_order(isin="IRO1SROD0001", side=1, config_section={"price": "200"})
    body = json.loads(po.body)
    assert body["price"] == "200.0"        # override, not the 9930 ceiling
    assert po.volume == 5000               # 1000000 // 200


# ---------------------------------------------------------------------------
# ExirAdapter — volume capped at the instrument max order quantity (mxqo)
# ---------------------------------------------------------------------------

def test_exir_buy_volume_capped_at_max_order_qty(monkeypatch):
    # BP-derived volume would be 1,000,000 // 200 = 5000, but the instrument's
    # max order quantity is 1000 → the order MUST be capped (broker rejects above
    # the volume upper threshold). This is the bug being fixed.
    _install_exir_fakes(monkeypatch, asset="1000000", max_qty=1000)
    a = exir_adapter.ExirAdapter("khobregan", "1164580090306", "pw")
    po = a.prepare_order(isin="IRO3SMBZ0001", side=1, config_section={"price": "200"})
    assert po.volume == 1000
    assert json.loads(po.body)["quantity"] == "1000"


def test_exir_sell_volume_capped_at_max_order_qty(monkeypatch):
    # Large holdings must also be capped to the per-order max.
    rows = [{"insMaxLcode": "IRO3SMBZ0001", "asset": 999999}]
    _install_exir_fakes(monkeypatch, holdings_rows=rows, max_qty=1000)
    a = exir_adapter.ExirAdapter("khobregan", "1164580090306", "pw")
    po = a.prepare_order(isin="IRO3SMBZ0001", side=2, config_section={"price": "200"})
    assert po.volume == 1000


def test_exir_no_cap_when_max_qty_zero(monkeypatch):
    # max_qty=0 means "unknown / no cap" — volume must NOT be clamped to 0/1.
    _install_exir_fakes(monkeypatch, asset="1000000", max_qty=0)
    a = exir_adapter.ExirAdapter("khobregan", "1164580090306", "pw")
    po = a.prepare_order(isin="IRO3SMBZ0001", side=1, config_section={"price": "200"})
    assert po.volume == 5000   # uncapped (1000000 // 200)


# ---------------------------------------------------------------------------
# EphoenixAdapter — delegates to EphoenixAPIClient, returns Bearer + no signer
# ---------------------------------------------------------------------------

class _FakeEphoenixClient:
    def __init__(self, **kw):
        self.kw = kw

    def authenticate(self):
        return "TOKEN123"

    def get_buying_power(self):
        return 5_000_000.0

    def get_instrument_info(self, isin):
        return {
            "title": "Sample", "symbol": "SMP",
            "max_price": 1000, "min_price": 900,
            "max_volume": 100_000,
        }

    def calculate_order_volume(self, isin, side, buying_power, price):
        return 4321

    def get_holdings(self, isin):
        return 888


def test_ephoenix_prepare_returns_bearer_no_signer(monkeypatch):
    monkeypatch.setattr(ephoenix_adapter, "EphoenixAPIClient", _FakeEphoenixClient)
    a = ephoenix_adapter.EphoenixAdapter("ayandeh", "u", "p", captcha_decoder=lambda b: "x")
    po = a.prepare_order(isin="IRO3SMBZ0001", side=1, config_section={})

    assert isinstance(po, PreparedOrder)
    assert po.bearer_token == "TOKEN123"
    assert po.signer is None
    assert po.cookies is None
    # BUY → max_price, volume = min(calc, max_volume) = min(4321, 100000) = 4321
    assert po.price == 1000
    assert po.volume == 4321
    body = json.loads(po.body)
    assert body == {
        "isin": "IRO3SMBZ0001", "side": 1, "validity": 1, "accountType": 1,
        "price": 1000, "volume": 4321, "validityDate": None, "serialNumber": 0,
    }
    assert po.order_url.endswith("/api/v2/orders/NewOrder")


def test_ephoenix_prepare_sell_uses_holdings_and_min_price(monkeypatch):
    monkeypatch.setattr(ephoenix_adapter, "EphoenixAPIClient", _FakeEphoenixClient)
    a = ephoenix_adapter.EphoenixAdapter("ayandeh", "u", "p", captcha_decoder=lambda b: "x")
    po = a.prepare_order(isin="IRO3SMBZ0001", side=2, config_section={})
    assert po.price == 900           # min_price on SELL
    assert po.volume == 888          # from holdings
    assert po.bearer_token == "TOKEN123" and po.signer is None


# ---------------------------------------------------------------------------
# Factory wiring
# ---------------------------------------------------------------------------

def test_get_adapter_picks_exir(monkeypatch):
    _install_exir_fakes(monkeypatch)
    a = get_adapter("khobregan", username="u", password="p",
                    config_section={"broker_family": "exir"},
                    captcha_decoder=lambda b: "12345", cache=None)
    assert isinstance(a, exir_adapter.ExirAdapter)
    assert a.family == "exir"


def test_get_adapter_picks_ephoenix_by_default():
    a = get_adapter("ayandeh", username="u", password="p",
                    config_section={}, captcha_decoder=lambda b: "x", cache=None)
    assert isinstance(a, ephoenix_adapter.EphoenixAdapter)
    assert a.family == "ephoenix"


# ---------------------------------------------------------------------------
# Plain-assert fallback runner (no pytest) — uses a tiny monkeypatch shim.
# ---------------------------------------------------------------------------

class _MiniMonkeypatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value=None, raising=True):
        # Support both setattr(obj, "name", value) and module-attr forms.
        old = getattr(target, name)
        self._undo.append((target, name, old))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            setattr(target, name, old)
        self._undo.clear()


def _run_all():
    import inspect
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = 0
    for name, fn in tests:
        params = inspect.signature(fn).parameters
        mp = _MiniMonkeypatch()
        try:
            if "monkeypatch" in params:
                fn(mp)
            else:
                fn()
            print(f"PASS {name}")
            passed += 1
        finally:
            mp.undo()
    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    import sys
    sys.exit(_run_all())
