"""Broker-family adapter package.

Each broker code belongs to a *family* (``ephoenix`` | ``exir``). The two
families speak fundamentally different protocols, so each is implemented as a
:class:`~app.services.brokers.base.BrokerAdapter`. The legacy
``broker_client`` module is now a thin dispatcher that resolves a code to its
family (via :mod:`app.services.brokers.registry`) and delegates to the right
adapter.

Public surface (import from here):

* :class:`BrokerAdapter`, :class:`VerifyResult`, :class:`IsinInfo` — the contract
* :func:`get_adapter` — code -> adapter instance
* :func:`family_of` — code -> family string (reads the warm cache)
"""
from __future__ import annotations

from app.services.brokers.base import BrokerAdapter, IsinInfo, VerifyResult
from app.services.brokers.registry import (
    UnknownBrokerError,
    family_of,
    get_adapter,
    set_family_map,
    warm_family_cache,
)

__all__ = [
    "BrokerAdapter",
    "VerifyResult",
    "IsinInfo",
    "get_adapter",
    "family_of",
    "warm_family_cache",
    "set_family_map",
    "UnknownBrokerError",
]
