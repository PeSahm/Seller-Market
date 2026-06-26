"""Shared public RLC / Tadbir market-data lookup (used by exir + onlineplus).

The ``getstockprice2`` handler on ``core.tadbirrlc.com`` is PUBLIC (no auth),
keyed by ISIN (``nc``), and is the SAME backend the exir AND onlineplus (Hafez)
families price + validate instruments against — confirmed live for both
(``hap`` ceiling / ``lap`` floor / ``mxqo`` max order qty / ``ltp``/``cp``/``pcp``
prices). Factoring it here keeps the two family adapters DRY without coupling
them. ``trust_env=False`` so a foreign HTTP proxy on the mgmt host never
intercepts the Iranian endpoint (Session-6 lesson).

The doubled slash after the host reproduces the official client's URL exactly.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Optional

import httpx

from app.services.brokers.base import IsinInfo

_HTTP_TIMEOUT_S = 20.0
_RLC_STOCK_INFO_URL = "https://core.tadbirrlc.com//StockInformationHandler"
_RLC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _rlc_blob(params: dict) -> str:
    """RLC-style single-quoted JSON-ish blob, URL-encoded, with the trailing
    ``&jsoncallback=`` (mirrors ``SellerMarket/rlc_market._blob``)."""
    inner = "{" + ",".join(f"'{k}':'{v}'" for k, v in params.items()) + "}"
    return urllib.parse.quote(inner) + "&jsoncallback="


async def rlc_instrument(isin: str) -> Optional[dict]:
    """Return the public ``getstockprice2`` market-data row for ``isin`` (the
    dict whose ``nc`` equals the ISIN), or ``None`` if the backend doesn't know
    it. Raises on a transport/HTTP failure (the caller turns that into a
    graceful ``ok=False`` rather than a 500)."""
    url = _RLC_STOCK_INFO_URL + "?" + _rlc_blob(
        {"Type": "getstockprice2", "la": "Fa", "arr": isin}
    )
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_S, trust_env=False
    ) as client:
        resp = await client.get(url, headers={"User-Agent": _RLC_UA})
        resp.raise_for_status()
        rows = json.loads(resp.text)
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("nc") == isin:
            return row
    return None


def build_isin_info(isin: str, row: dict) -> IsinInfo:
    """Build a successful :class:`IsinInfo` from a ``getstockprice2`` row.

    ``sf`` = symbol, ``cn`` = Persian name, ``hap`` = ceiling (max price),
    ``lap`` = floor (min price), ``ltp``/``cp``/``pcp`` = last/close prices,
    ``mxqo`` = max order qty. Only strictly-positive numbers are kept (a 0/blank
    band means "no live band" rather than a real bound).
    """
    def _num(*keys: str) -> Optional[float]:
        for k in keys:
            try:
                val = float(row.get(k))
            except (TypeError, ValueError):
                continue
            if val > 0:
                return val
        return None

    symbol = str(row.get("sf")).strip() or None if row.get("sf") is not None else None
    title = str(row.get("cn")).strip() or None if row.get("cn") is not None else None
    mxqo = _num("mxqo")
    return IsinInfo(
        ok=True,
        isin=isin,
        symbol=symbol,
        title=title,
        last_price=_num("ltp", "cp", "pcp"),
        min_price=_num("lap"),
        max_price=_num("hap"),
        max_volume=int(mxqo) if mxqo else None,
        message="Instrument confirmed via market data.",
    )


async def isin_info(isin: str) -> IsinInfo:
    """Validate ``isin`` against the public RLC market-data backend.

    Returns a populated ``ok=True`` :class:`IsinInfo` on a hit, or ``ok=False``
    with ``.error`` (the verify partial renders ``.error``) on a blank/unknown
    ISIN or an unreachable backend — never raises, so a typo'd code can't look
    verified and a backend blip can't 500 the verify route.
    """
    isin = (isin or "").strip()
    if not isin:
        return IsinInfo(ok=False, isin=isin, error="No ISIN provided.")
    try:
        row = await rlc_instrument(isin)
    except Exception as exc:  # noqa: BLE001 — never raise out of verify
        return IsinInfo(
            ok=False,
            isin=isin,
            error=f"Could not reach market data to validate the ISIN: {exc}",
        )
    if row is None:
        return IsinInfo(
            ok=False,
            isin=isin,
            error="ISIN not found in market data — check the code.",
        )
    return build_isin_info(isin, row)
