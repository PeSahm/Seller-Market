"""Ephoenix-family broker adapter.

A thin wrapper around the existing ephoenix implementation that still lives in
``app.services.broker_client`` (the ``_ephoenix_*`` functions). We deliberately
did NOT move that code out of ``broker_client`` — tests patch internals there,
and the public dispatchers in ``broker_client`` route ephoenix codes straight
to the ``_ephoenix_*`` bodies. This adapter exists so the registry's
``get_adapter`` can return a uniform :class:`~app.services.brokers.base.BrokerAdapter`
for either family.

Import note: ``broker_client`` imports ``base`` and ``registry`` at module top
but only imports the family adapters (``ephoenix``/``exir``) lazily, so there is
no import cycle even though this module imports ``broker_client`` at the top.
"""
from __future__ import annotations

from typing import Optional

from app.services import broker_client
from app.services.brokers.base import IsinInfo, VerifyResult


class EphoenixAdapter:
    """Adapter for the ephoenix broker family (the 11 current brokers)."""

    family = "ephoenix"

    def __init__(self, code: str):
        self.code = code

    async def verify_credentials(
        self, username: str, password: str, ocr_service_url: str
    ) -> VerifyResult:
        return await broker_client._ephoenix_verify_credentials(
            self.code, username, password, ocr_service_url
        )

    async def verify_isin(
        self, username: str, password: str, isin: str, ocr_service_url: str
    ) -> IsinInfo:
        return await broker_client._ephoenix_verify_isin(
            self.code, username, password, isin, ocr_service_url
        )

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
        return await broker_client._ephoenix_get_orders(
            self.code,
            username,
            password,
            ocr_service_url,
            from_date=from_date,
            to_date=to_date,
            side=side,
            isin=isin,
            include_status=include_status,
            page_size=page_size,
            max_pages=max_pages,
        )

    async def get_holdings(
        self, username: str, password: str, isin: str, *, ocr_service_url: str
    ) -> int:
        return await broker_client._ephoenix_get_holdings(
            self.code, username, password, isin, ocr_service_url=ocr_service_url
        )
