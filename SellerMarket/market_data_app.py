"""Per-host market-data sidecar (Flask).

One container per VPS, deployed by the mgmt UI. Serves market-wide data — price
band, last price, instrument list, and the order queue — to every local bot and
the local mgmt UI from the single shared RLC backend (see :mod:`rlc_market`).
Keeps each host self-contained: no cross-VPS dependency, no tsetmc.

All data endpoints hit the public RLC backend (no per-request auth). A reference
Exir account (Khobregan, from ``MARKET_DATA_*`` env, injected by the mgmt UI from
the encrypted ``market_data_account`` setting) is held available for any endpoint
that later proves to need an authenticated session; ``/health`` reports whether
it is configured.

Endpoints (all GET, JSON):
    /health                      → {status, account_configured}
    /price-band?isin=            → {isin, ceiling, floor}
    /last-price?isin=            → {isin, last_price}
    /queue?isin=                 → {isin, buy_volume, sell_volume, ...} | 404
    /instruments                 → {count, instruments:[{isin,symbol,name}]}
    /search?q=&limit=            → {count, instruments:[...]}

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import logging
import os

from flask import Flask, jsonify, request

import json
import queue as _queuelib  # NOT ``queue`` — the /queue route fn shadows that name
import threading

import rlc_market

logging.basicConfig(
    level=os.environ.get("MARKET_DATA_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("market_data")

app = Flask(__name__)

# Reference account (optional) — present so an authenticated RLC/Exir call can be
# added later without redeploying the wiring. The data endpoints don't need it.
# The auto-sell WS fan-out (/ws/queue, #110) DOES need it: the broker/username/
# password authenticate the single upstream Khobregan WebSocket.
_ACCOUNT = {
    "broker": os.environ.get("MARKET_DATA_BROKER") or "",
    "username": os.environ.get("MARKET_DATA_USERNAME") or "",
    "password": os.environ.get("MARKET_DATA_PASSWORD") or "",
}


class _QueueHub:
    """Fan-out from ONE upstream Khobregan WS to many local /ws/queue subscribers.

    Local subscribers for an ISIN share a single upstream subscription (one
    ``rlc_ws.RlcQueueClient``). ``publish`` pushes each ``buy_volume`` to every
    local subscriber's bounded queue (dropping on a slow consumer, never blocking
    the upstream reader).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._subs: dict[str, set] = {}
        self._latest: dict[str, object] = {}
        self._client = None

    def _ensure_client(self):
        with self._lock:
            if self._client is not None:
                return self._client
            if not (_ACCOUNT["broker"] and _ACCOUNT["username"] and _ACCOUNT["password"]):
                raise RuntimeError("market-data account not configured (MARKET_DATA_*)")
            import rlc_ws
            from captcha_utils import decode_captcha
            self._client = rlc_ws.RlcQueueClient(
                tenant=_ACCOUNT["broker"],
                username=_ACCOUNT["username"],
                password=_ACCOUNT["password"],
                decode_captcha=decode_captcha,
                on_update=self.publish,
            )
            return self._client

    def subscribe_local(self, isin: str) -> "_queuelib.Queue":
        # Resolve the upstream client FIRST: if the account isn't configured this
        # raises before we register the queue, so a failed subscribe can't leave
        # an orphan in ``_subs``.
        client = self._ensure_client()
        q: "_queuelib.Queue" = _queuelib.Queue(maxsize=8)
        with self._lock:
            self._subs.setdefault(isin, set()).add(q)
            latest = self._latest.get(isin)
        client.subscribe(isin)  # idempotent upstream subscribe
        if latest is not None:
            try:
                q.put_nowait(latest)
            except _queuelib.Full:
                pass
        return q

    def unsubscribe_local(self, isin: str, q: "_queuelib.Queue") -> None:
        with self._lock:
            subs = self._subs.get(isin)
            if subs:
                subs.discard(q)

    def publish(self, isin: str, buy_volume) -> None:
        with self._lock:
            self._latest[isin] = buy_volume
            subs = list(self._subs.get(isin, ()))
        for q in subs:
            try:
                q.put_nowait(buy_volume)
            except _queuelib.Full:
                pass  # slow local consumer — drop this tick


_HUB = _QueueHub()

# Local push transport (flask-sock). Optional so the REST endpoints still import
# + run if the dep is missing; /ws/queue just won't be registered.
try:
    from flask_sock import Sock
    _sock = Sock(app)
except Exception:  # noqa: BLE001
    _sock = None
    logger.warning("flask-sock not available — /ws/queue (auto-sell push) disabled")


def _isin_arg() -> str:
    return (request.args.get("isin") or "").strip().upper()


@app.get("/health")
def health():
    return jsonify(
        status="ok",
        account_configured=bool(_ACCOUNT["username"] and _ACCOUNT["password"]),
    )


@app.get("/price-band")
def price_band():
    isin = _isin_arg()
    if not isin:
        return jsonify(error="isin required"), 400
    try:
        ceiling, floor = rlc_market.get_price_band(isin)
    except Exception as exc:  # noqa: BLE001 — never 500 the sidecar
        logger.warning("price-band %s failed: %s", isin, exc)
        return jsonify(error="no price band", isin=isin), 404
    return jsonify(isin=isin, ceiling=ceiling, floor=floor)


@app.get("/last-price")
def last_price():
    isin = _isin_arg()
    if not isin:
        return jsonify(error="isin required"), 400
    last = rlc_market.get_last_price(isin)
    if not last:
        return jsonify(error="no last price", isin=isin), 404
    return jsonify(isin=isin, last_price=last)


@app.get("/queue")
def queue():
    isin = _isin_arg()
    if not isin:
        return jsonify(error="isin required"), 400
    q = rlc_market.get_queue(isin)
    if q is None:
        return jsonify(error="no queue data", isin=isin), 404
    return jsonify(isin=isin, **q)


@app.get("/instruments")
def instruments():
    rows = rlc_market.get_instruments()
    return jsonify(count=len(rows), instruments=rows)


@app.get("/search")
def search():
    q = request.args.get("q") or ""
    try:
        limit = max(1, min(100, int(request.args.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    rows = rlc_market.search_instruments(q, limit=limit)
    return jsonify(count=len(rows), instruments=rows)


if _sock is not None:
    @_sock.route("/ws/queue")
    def ws_queue(ws):
        """Stream live ``{isin, buy_volume}`` for ``?isin=`` to one local bot (#110).

        Subscribes the ISIN upstream (shared) and forwards each best-buy-queue
        update. A 30s keepalive ``{"ping": true}`` is sent during quiet periods so
        the bot's client distinguishes "idle" from "disconnected".
        """
        isin = (request.args.get("isin") or "").strip().upper()
        if not isin:
            return
        try:
            q = _HUB.subscribe_local(isin)
        except Exception as exc:  # noqa: BLE001
            logger.warning("/ws/queue subscribe failed for %s: %s", isin, exc)
            return
        try:
            while True:
                try:
                    bv = q.get(timeout=30)
                except _queuelib.Empty:
                    ws.send(json.dumps({"isin": isin, "ping": True}))
                    continue
                ws.send(json.dumps({"isin": isin, "buy_volume": bv}))
        except Exception:  # noqa: BLE001 — client gone / send failed
            pass
        finally:
            _HUB.unsubscribe_local(isin, q)


def main() -> None:
    port = int(os.environ.get("MARKET_DATA_PORT") or 8077)
    logger.info(
        "market-data sidecar starting on :%d (account_configured=%s)",
        port, bool(_ACCOUNT["username"]),
    )
    # Threaded so several local bots can poll the queue concurrently. Internal
    # service on the host network — no external exposure.
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
