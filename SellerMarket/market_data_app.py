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

import rlc_market

logging.basicConfig(
    level=os.environ.get("MARKET_DATA_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("market_data")

app = Flask(__name__)

# Reference account (optional) — present so an authenticated RLC/Exir call can be
# added later without redeploying the wiring. The data endpoints don't need it.
_ACCOUNT = {
    "broker": os.environ.get("MARKET_DATA_BROKER") or "",
    "username": os.environ.get("MARKET_DATA_USERNAME") or "",
    "password": os.environ.get("MARKET_DATA_PASSWORD") or "",
}


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
