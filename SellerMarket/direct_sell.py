"""Fire one prepared order with a direct HTTP POST — NOT through locust (#110).

The locust hot path (``locustfile_new.place_order``) turns a ``PreparedOrder``
into one POST during the market-open burst. Auto-sell is a deliberate,
out-of-burst action, so it places orders directly with ``requests`` instead of
the locust client — but with the BYTE-IDENTICAL request shape so behaviour
matches the proven path:

* ephoenix   → ``Authorization: Bearer <token>`` header.
* exir       → session ``cookies`` + a FRESH ``X-App-N`` signature (``signer()``)
  recomputed at send time, second-granular.
* onlineplus → session ``cookies`` only (no Bearer, no signer).

The body is the pre-encoded JSON string from ``prepare_order`` /
``prepare_chunk`` and is sent as ``data=`` (not ``json=``), exactly like the
locust task.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import logging

import requests

from broker_adapters import PreparedOrder

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Default poster: a dedicated session that reaches the broker DIRECTLY (never via
# a foreign HTTP proxy in the host env) — same hardening rlc_price/rlc_market use.
_DIRECT = requests.Session()
_DIRECT.trust_env = False


def send_prepared_order(
    prepared: PreparedOrder,
    *,
    session: requests.Session | None = None,
    timeout: float = 10.0,
) -> tuple[int, bytes]:
    """POST one ``PreparedOrder`` directly. Returns ``(status_code, body_bytes)``.

    Dispatches on which auth field the family populated (mirrors
    ``locustfile_new.place_order``): ``signer`` set ⇒ exir cookies + a fresh
    ``X-App-N`` header; else ``cookies`` set ⇒ onlineplus cookie-only; else ⇒
    ephoenix static Bearer. Raises ``requests.RequestException`` on a transport
    failure (the caller logs + treats the chunk as not-fired; it re-fires on the
    next push).
    """
    poster = (session or _DIRECT).post
    if prepared.signer is not None:
        # exir — cookies + a fresh per-request X-App-N (recomputed NOW).
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _UA,
        }
        headers.update(prepared.signer())
        resp = poster(
            prepared.order_url,
            data=prepared.body,
            headers=headers,
            cookies=prepared.cookies,
            timeout=timeout,
        )
    elif prepared.cookies is not None:
        # onlineplus — cookie session only (NO Bearer, NO signer).
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _UA,
        }
        resp = poster(
            prepared.order_url,
            data=prepared.body,
            headers=headers,
            cookies=prepared.cookies,
            timeout=timeout,
        )
    else:
        # ephoenix — static Bearer.
        headers = {
            "authorization": f"Bearer {prepared.bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _UA,
        }
        resp = poster(prepared.order_url, data=prepared.body, headers=headers, timeout=timeout)
    return resp.status_code, resp.content


__all__ = ["send_prepared_order"]
