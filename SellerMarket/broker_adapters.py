"""Broker-adapter abstraction for the trading bot (Phase 2 of the Exir feature).

This is the bot-side seam that lets one locust hot path drive two structurally
different broker families:

* **ephoenix** — static ``Authorization: Bearer`` header; order body is the
  ISIN-keyed NewOrder payload. The adapter is a thin wrapper around the existing
  :class:`EphoenixAPIClient` flow, reproducing today's ``prepare_order_data``
  byte-for-byte so nothing about the live ephoenix path changes.
* **exir** — cookie session + a per-request, second-granular ``X-App-N``
  signature (no Bearer). Price has no instrument endpoint, so it is carried in
  config.

The seam is :class:`PreparedOrder`: a family-agnostic description of exactly
one order request. The hot-path caller (in ``locustfile_new.py``, owned by the
parent agent) reads ``order_url``/``body`` and applies whichever auth mechanism
is populated — ``bearer_token`` for ephoenix, or ``signer()`` + ``cookies`` for
exir. Whichever the family doesn't use is ``None``.

This package is a FLAT layout (the Dockerfile does ``COPY *.py ./``), so this is
a top-level module, not a sub-package.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

from broker_enum import BrokerCode


def cookies_to_dict(jar) -> dict:
    """Flatten a cookie jar to ``{name: value}`` WITHOUT triggering a
    CookieConflict.

    ``dict(jar)`` (and ``jar.items()``) reach the jar through its name-keyed
    mapping interface, which RAISES ``CookieConflictError``/``CookieConflict``
    when a load balancer set two cookies with the SAME name on different
    paths/domains — exactly the F5 BIG-IP ``f5avr…_session_`` pair in front of
    Hafez (OnlinePlus). Iterating the jar's ``Cookie`` objects directly is
    duplicate-safe; the unique-named auth cookie (e.g. ``AuthCookie_OnlineCookie``
    / exir's session cookie) is preserved. Accepts any ``requests`` /
    ``http.cookiejar`` jar (iterating yields ``Cookie`` objects)."""
    return {c.name: c.value for c in jar}


@dataclass
class PreparedOrder:
    """Family-agnostic description of a single ready-to-fire order request.

    The locust hot path turns this into one HTTP POST. Exactly one auth
    mechanism is populated per family; the other fields are ``None``:

    * ephoenix → ``bearer_token`` set, ``signer``/``cookies`` ``None``.
    * exir     → ``signer`` + ``cookies`` set, ``bearer_token`` ``None``.
    """

    order_url: str
    body: str                                 # json-encoded order payload
    bearer_token: Optional[str]               # ephoenix: the Bearer; exir: None
    signer: Optional[Callable[[], dict]]      # exir: ()->{"X-App-N": ...}; ephoenix: None
    cookies: Optional[dict]                   # exir: session cookies for self.client; ephoenix: None
    price: float
    volume: int


@dataclass
class SellContext:
    """Everything ``auto_sell_engine.sell_entire_position`` needs for one (account,
    isin), built once per auto-sell trigger by ``BrokerAdapter.open_sell_context``.

    Holds the day's FLOOR price + the per-order max volume, plus two ready-bound
    callables that hide all family specifics:

    * ``fetch_holdings()`` → the customer's CURRENT whole-share holding (LIVE).
    * ``prepare_chunk(volume)`` → a :class:`PreparedOrder` to SELL ``volume`` at
      the floor (auth already resolved; exir signer fresh per call).
    """

    floor_price: int
    max_order_volume: int                     # per-order cap; 0 = unknown / no cap
    fetch_holdings: Callable[[], int]
    prepare_chunk: Callable[[int], "PreparedOrder"]


class BrokerAdapter(ABC):
    """Common contract for a broker family.

    Subclasses set :attr:`family` and implement :meth:`prepare_order`, which does
    all the slow, network-bound work (auth, sizing, payload build) OFF the hot
    path and returns a :class:`PreparedOrder`. ``prepare_order`` is allowed to
    raise on any failure (auth/credentials/no-holdings/missing-price); the caller
    marks the locust user failed, exactly as the current ephoenix auth failure
    does today.
    """

    family: str

    @abstractmethod
    def prepare_order(self, *, isin: str, side: int, config_section: dict) -> PreparedOrder:
        ...

    def open_sell_context(self, *, isin: str, config_section: dict) -> "SellContext":
        """Build a :class:`SellContext` for auto-sell (#110).

        Authenticates once, reads the floor price + per-order max volume, and
        returns the bound ``fetch_holdings`` / ``prepare_chunk`` callables. May
        raise on any auth / price / config failure (the monitor logs + holds).
        Non-abstract so existing/3rd-party adapters keep instantiating; a family
        without an override simply can't auto-sell.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support auto-sell"
        )


def resolve_family(broker_code: str, config_section: dict) -> str:
    """Resolve the broker family for ``broker_code``.

    Data-driven: trust ``config_section['broker_family']`` when present (the mgmt
    UI renders it per stack), otherwise fall back to
    :meth:`BrokerCode.family`, which defaults to ``"ephoenix"``.
    """
    fam = (config_section or {}).get("broker_family")
    if fam:
        return str(fam).strip().lower()
    return BrokerCode.family(broker_code)


def is_auto_sell_only(section) -> bool:
    """True when the config section is flagged ``auto_sell_only = true``.

    Such sections exist purely to arm the auto-sell monitor for an EXISTING
    holding (no buy) — they must never fire an order at market open, so the
    locust user-class builder and cache_warmup skip them. Sections arrive as
    plain dicts (not ConfigParser proxies, so no ``getboolean``); the truthy
    strings are matched manually. Missing key / empty / anything else → False.
    """
    raw = (section or {}).get("auto_sell_only")
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def get_adapter(
    broker_code: str,
    *,
    username: str,
    password: str,
    config_section: dict,
    captcha_decoder: Callable[[str], str],
    cache: Any = None,
) -> BrokerAdapter:
    """Factory: pick + construct the adapter for ``broker_code``.

    Resolves the family (config-first, enum fallback) and returns the matching
    adapter. Imports of the concrete adapters are local so importing this module
    stays cheap and avoids any import cycle.
    """
    family = resolve_family(broker_code, config_section)

    if family == "exir":
        from exir_adapter import ExirAdapter

        return ExirAdapter(
            broker_code=broker_code,
            username=username,
            password=password,
            captcha_decoder=captcha_decoder,
            cache=cache,
        )

    if family == "onlineplus":
        from onlineplus_adapter import OnlinePlusAdapter

        return OnlinePlusAdapter(
            broker_code=broker_code,
            username=username,
            password=password,
            captcha_decoder=captcha_decoder,
            cache=cache,
            # OnlinePlus reads its per-broker base_domain from the rendered
            # config.ini section (tenants don't share a host convention).
            config_section=config_section,
        )

    # Default / "ephoenix": preserve today's behaviour for every known broker.
    from ephoenix_adapter import EphoenixAdapter

    return EphoenixAdapter(
        broker_code=broker_code,
        username=username,
        password=password,
        captcha_decoder=captcha_decoder,
        cache=cache,
    )
