"""Broker-adapter contract + the shared result dataclasses.

``VerifyResult`` and ``IsinInfo`` were historically defined in
``app.services.broker_client``; they live here now so both family adapters
(ephoenix, exir) and the thin ``broker_client`` dispatcher can share them.
``broker_client`` re-exports them for backwards compatibility, so existing
callers/tests that do ``from app.services.broker_client import VerifyResult``
keep working.

The field sets are kept BYTE-FOR-BYTE compatible with the old dataclasses so
the verify partials/templates that read ``.full_name`` / ``.national_id`` /
``.symbol`` etc. are unaffected.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass
class VerifyResult:
    """Outcome of a credential verification.

    Exactly one of ``ok=True`` (with ``full_name`` populated) or ``ok=False``
    (with ``error`` populated) holds. The other broker-side sanity fields are
    populated only on success — and may be ``None`` for families (e.g. Exir)
    that don't expose a ``getcustomerinfo`` record; the verify partials must
    degrade gracefully when they're ``None``.
    """

    ok: bool
    full_name: Optional[str] = None
    national_id: Optional[str] = None
    bourse_code: Optional[str] = None
    type_: Optional[str] = None
    message: Optional[str] = None  # broker's human-readable status, even on success
    error: Optional[str] = None  # operator-facing error explanation


@dataclass
class IsinInfo:
    """Outcome of an instrument lookup against the broker.

    Same ``ok=True`` / ``ok=False`` shape as :class:`VerifyResult`. For
    symbol-based families that don't expose an instrument-metadata endpoint in
    Phase 1, ``ok=True`` may be returned with only ``isin``/``symbol`` echoed
    and a ``message`` explaining metadata wasn't fetched.
    """

    ok: bool
    isin: Optional[str] = None
    symbol: Optional[str] = None
    title: Optional[str] = None
    last_price: Optional[float] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    max_volume: Optional[int] = None
    min_volume: Optional[int] = None
    error: Optional[str] = None
    message: Optional[str] = None


@runtime_checkable
class BrokerAdapter(Protocol):
    """The capability contract the mgmt UI needs from any broker family.

    An adapter instance is bound to a single broker ``code`` (e.g. ``"gs"`` or
    ``"khobregan"``); the family is fixed by the concrete class. All methods are
    async and accept the operator-typed credentials + the global OCR service URL
    (captcha solving is shared across families).

    The public dispatcher in ``app.services.broker_client`` keeps the historical
    keyword names (notably ``isin=``); adapters interpret that argument as their
    instrument key (ISIN for ephoenix, ``insMaxLCode`` for exir — which is also
    ISIN-shaped on the tenants observed).
    """

    code: str
    family: str

    async def verify_credentials(
        self, username: str, password: str, ocr_service_url: str
    ) -> VerifyResult:
        ...

    async def verify_isin(
        self, username: str, password: str, isin: str, ocr_service_url: str
    ) -> IsinInfo:
        ...

    async def get_orders(
        self,
        username: str,
        password: str,
        ocr_service_url: str,
        *,
        from_date: str,
        to_date: str,
        side: Optional[int] = None,
        isin: Optional[str] = None,
        include_status: Optional[list[int]] = None,
        page_size: int = 100,
        max_pages: int = 500,
    ) -> tuple[list[dict], Optional[str]]:
        ...
