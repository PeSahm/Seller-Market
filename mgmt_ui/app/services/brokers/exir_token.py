"""Exir / Rayan HamAfza ``X-App-N`` per-request signature.

Every authenticated read against ``{code}.exirbroker.com`` must carry an
``X-App-N`` header derived from the login seed ``nt`` and the *full request
path including its query string*. The signature changes every second (it is a
function of the current UTC wall-clock time), so it MUST be recomputed
immediately before each request — see :func:`build_app_n`.

This mirrors the decompiled web-platform ``BuildHeaders(text3)`` and was
proven against a live HTTP 200 on ``orderbookReport`` (see
``SellerMarket/scratch/EXIR_FINDINGS.md``):

* **time basis = UTC** (``datetime.utcnow()`` equivalent), and
* **the signed string is the FULL path INCLUDING the query string**, e.g.
  ``/api/v1/user/orderbookReport?size=1000&startDate=...&orderStatusId=3``.
"""
from __future__ import annotations

from datetime import datetime, timezone


def build_app_n(nt: str, path_with_query: str, now: datetime | None = None) -> str:
    """Compute the ``X-App-N`` header value for one request.

    Args:
        nt: the 130-char numeric seed returned by ``/api/v2/login``.
        path_with_query: the request path INCLUDING the query string
            (e.g. ``/api/v2/user/buyingPower`` or
            ``/api/v1/user/orderbookReport?size=1000&...``).
        now: override for the time basis (UTC). Defaults to the current UTC
            time. Pass an explicit value only for tests — production callers
            must let it default so the signature tracks wall-clock seconds.

    Returns:
        ``"<digits>.<digits>"`` — the header value.
    """
    now = now or datetime.now(timezone.utc)
    text = nt[2:]
    if len(text) - 5 <= 0:
        raise ValueError("nt too short")
    char_sum = sum(ord(c) for c in path_with_query)
    t = 3600 * now.hour + 60 * now.minute + now.second
    idx = abs(t % (len(text) - 5) - int(nt[0:2]))
    return f"{int(text[idx:idx + 5]) * t * char_sum}.{t * char_sum}"
