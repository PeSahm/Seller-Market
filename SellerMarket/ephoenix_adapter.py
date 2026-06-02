"""Ephoenix-family broker adapter (Phase 2 of the Exir feature).

This wraps the existing :class:`EphoenixAPIClient` flow and reproduces the
current ``prepare_order_data`` ephoenix logic BYTE-FOR-BYTE, so routing ephoenix
brokers through the new adapter seam changes nothing about the live path:

* same broker validation (:meth:`BrokerCode.is_valid`),
* same endpoints (``BrokerCode(broker_code).get_endpoints()``),
* same client construction + ``authenticate`` → token,
* same buying-power / instrument-info fetch,
* same price selection (max_price on BUY, min_price on SELL),
* same volume sizing (CalculateOrderParam on BUY, holdings on SELL),
* same NewOrder payload + ``json.dumps``.

The only structural change is the return type: a :class:`PreparedOrder` carrying
the Bearer token (``signer``/``cookies`` are ``None`` for ephoenix). The
client is built lazily inside :meth:`prepare_order`, mirroring today where
``prepare_order_data`` constructs a fresh ``EphoenixAPIClient`` per call.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from broker_adapters import BrokerAdapter, PreparedOrder
from broker_enum import BrokerCode
from api_client import EphoenixAPIClient

logger = logging.getLogger(__name__)


class EphoenixAdapter(BrokerAdapter):
    """Adapter for the ephoenix.ir / ibtrader.ir broker family."""

    family = "ephoenix"

    def __init__(
        self,
        broker_code: str,
        username: str,
        password: str,
        captcha_decoder: Callable[[str], str],
        cache: Optional[Any] = None,
    ):
        self.broker_code = broker_code
        self.username = username
        self.password = password
        self.captcha_decoder = captcha_decoder
        self.cache = cache

    def prepare_order(self, *, isin: str, side: int, config_section: dict) -> PreparedOrder:
        """Prepare one ephoenix order — verbatim port of ``prepare_order_data``."""
        username = self.username
        password = self.password
        broker_code = self.broker_code
        side = int(side)

        logger.info(f"{'='*80}")
        logger.info(f"Preparing order for {username}@{broker_code} - ISIN: {isin}")
        logger.info(f"{'='*80}")

        # Validate broker code
        if not BrokerCode.is_valid(broker_code):
            raise ValueError(f"Invalid broker code: {broker_code}")

        # Get broker endpoints
        broker_enum = BrokerCode(broker_code)
        endpoints = broker_enum.get_endpoints()

        logger.info(f"Broker: {BrokerCode.get_broker_name(broker_code)}")

        # Initialize API client with cache
        api_client = EphoenixAPIClient(
            broker_code=broker_code,
            username=username,
            password=password,
            captcha_decoder=self.captcha_decoder,
            endpoints=endpoints,
            cache=self.cache,
        )

        # Step 1: Authenticate
        logger.info("Step 1: Authenticating...")
        try:
            token = api_client.authenticate()
            logger.info("✓ Authentication successful")
        except Exception as e:
            logger.error(f"❌ Authentication failed for {username}@{broker_code}: {e}")
            if broker_code == 'gs':
                logger.warning("⚠️  GS broker captcha issue - skipping this account")
            raise  # This will mark the task as failed in Locust

        # Step 2: Get buying power
        logger.info("Step 2: Fetching buying power...")
        try:
            buying_power = api_client.get_buying_power()
            logger.info(f"✓ Buying power: {buying_power:,.0f} Rials")
        except Exception as e:
            logger.error(f"❌ Failed to get buying power: {e}")
            raise

        # Step 3: Get instrument information
        logger.info("Step 3: Fetching instrument information...")
        instrument_info = api_client.get_instrument_info(isin)
        logger.info(f"✓ Instrument: {instrument_info['title']} ({instrument_info['symbol']})")

        # Determine price based on side
        if side == 1:  # Buy
            price = instrument_info['max_price']
            logger.info(f"✓ Buy order - Using max price: {price:,}")
        else:  # Sell
            price = instrument_info['min_price']
            logger.info(f"✓ Sell order - Using min price: {price:,}")

        # Step 4: Calculate volume — the formula differs by side.
        # BUY:  volume sourced from buying power (how much cash we can spend ÷ price),
        #       routed through the broker's CalculateOrderParam endpoint.
        # SELL: volume sourced from real portfolio holdings — buying power is
        #       meaningless for sells. See issue #59.
        logger.info("Step 4: Calculating order volume...")
        max_volume = instrument_info['max_volume']

        if side == 1:  # Buy
            calculated_volume = api_client.calculate_order_volume(
                isin=isin,
                side=side,
                buying_power=buying_power,
                price=price
            )
            volume = min(calculated_volume, max_volume)
            if volume != calculated_volume:
                logger.warning(f"⚠ BUY volume constrained from {calculated_volume:,} to {volume:,} (max allowed per order)")
            else:
                logger.info(f"✓ BUY volume: {volume:,} shares")
        else:  # Sell
            holdings = api_client.get_holdings(isin)
            if holdings <= 0:
                # Fail-fast: shipping a zero-volume order would either be rejected
                # by the broker or silently succeed as a no-op. Better to mark the
                # task failed in Locust so the operator sees it in the run summary.
                raise ValueError(f"no holdings for {isin} ({username}@{broker_code}); cannot sell")
            volume = min(holdings, max_volume)
            capped = " (capped by max_volume per order)" if volume < holdings else ""
            logger.info(f"✓ SELL volume sourced from holdings={holdings:,}, "
                       f"max_volume={max_volume:,} → {volume:,}{capped}")

        # Step 5: Prepare order payload
        logger.info("Step 5: Preparing order payload...")

        order_payload = {
            'isin': isin,
            'side': side,
            'validity': 1,  # Day order
            'accountType': 1,  # Default account
            'price': price,
            'volume': volume,
            'validityDate': None,
            'serialNumber': 0  # New order
        }

        order_json = json.dumps(order_payload)

        logger.info("✓ Order prepared:")
        logger.info(f"  ISIN: {isin}")
        logger.info(f"  Side: {'Buy' if side == 1 else 'Sell'}")
        logger.info(f"  Price: {price:,} Rials")
        logger.info(f"  Volume: {volume:,} shares")
        logger.info(f"  Total: {price * volume:,.0f} Rials")
        logger.info(f"{'='*80}\n")

        return PreparedOrder(
            order_url=endpoints['order'],
            body=order_json,
            bearer_token=token,
            signer=None,
            cookies=None,
            price=price,
            volume=volume,
        )
