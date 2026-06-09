"""Hermetic tests for the direct (non-locust) order sender (#110).

No network — a fake session captures the POST. Asserts the ephoenix vs exir
request shape matches ``locustfile_new.place_order``.
Run: ``python -m pytest test_direct_sell.py -q``.
"""
from __future__ import annotations

from broker_adapters import PreparedOrder
from direct_sell import send_prepared_order


class _FakeResp:
    def __init__(self, status=200, content=b"ok"):
        self.status_code = status
        self.content = content


class _FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResp()


def test_ephoenix_sends_bearer_no_cookies():
    p = PreparedOrder(
        order_url="https://api-x.ephoenix.ir/order",
        body='{"isin":"IRO1X","side":2}',
        bearer_token="TOK123",
        signer=None,
        cookies=None,
        price=5,
        volume=100,
    )
    sess = _FakeSession()
    status, body = send_prepared_order(p, session=sess)
    assert status == 200 and body == b"ok"
    url, kwargs = sess.calls[0]
    assert url == p.order_url
    assert kwargs["data"] == p.body                       # body sent as data=, not json=
    assert kwargs["headers"]["authorization"] == "Bearer TOK123"
    assert "X-App-N" not in kwargs["headers"]
    assert "cookies" not in kwargs                         # ephoenix sends no cookies


def test_exir_sends_cookies_and_fresh_signature():
    sign_calls = {"n": 0}

    def signer():
        sign_calls["n"] += 1
        return {"X-App-N": "12345.678"}

    p = PreparedOrder(
        order_url="https://khobregan.exirbroker.com/api/v1/order",
        body='{"insMaxLcode":"IRO1X","side":"SIDE_SALE"}',
        bearer_token=None,
        signer=signer,
        cookies={"JWT-TOKEN": "abc"},
        price=5,
        volume=100,
    )
    sess = _FakeSession()
    status, body = send_prepared_order(p, session=sess)
    assert status == 200
    url, kwargs = sess.calls[0]
    assert url == p.order_url                               # correct endpoint
    assert kwargs["data"] == p.body
    assert kwargs["headers"]["X-App-N"] == "12345.678"     # fresh signature applied
    assert "authorization" not in kwargs["headers"]
    assert kwargs["cookies"] == {"JWT-TOKEN": "abc"}       # exir carries cookies
    assert sign_calls["n"] == 1                            # signer called once, at send time


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
