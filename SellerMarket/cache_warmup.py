"""
Cache Warmup Script for Stock Trading Bot

This script pre-fetches and caches all necessary data before market opens,
ensuring instant execution when trading begins.

Usage:
    python cache_warmup.py --config config.ini
    
The script will:
1. Load all accounts from configuration
2. Authenticate all accounts and cache tokens
3. Fetch buying power for all accounts
4. Fetch instrument info for all configured stocks
5. Pre-calculate order parameters
6. Display cache statistics

Run this script 5-10 minutes before market opens to ensure all data is fresh.
"""

import argparse
import configparser
import logging
from typing import Dict
from datetime import datetime

from broker_enum import get_endpoints_for
from api_client import EphoenixAPIClient
from cache_manager import TradingCache
from captcha_utils import decode_captcha

# Configure logging - truncate log file on each run
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('cache_warmup.log', mode='w', encoding='utf-8'),  # mode='w' truncates file
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


def _warmup_exir(config_section: Dict[str, str], cache: TradingCache) -> bool:
    """Warm up / validate an Exir (Rayan-HamAfza) account.

    Exir uses a cookie + per-request ``X-App-N`` session (not the ephoenix Bearer
    token cache), and its prices come from the broker's own RLC band handler —
    none of which is market-hours gated. We exercise the FULL prepare path
    (login → buying power → RLC price band → buy fee → volume) exactly as the
    locust run will, WITHOUT placing an order, so the account can be validated
    ahead of the open. The adapter keeps its own in-memory session/price caches
    (it does not use the on-disk ``TradingCache``), so this is a health check /
    validation rather than a cross-process pre-cache.
    """
    from broker_adapters import get_adapter

    username = config_section['username']
    broker_code = config_section['broker']
    password = config_section['password']
    isin = config_section['isin']
    side = int(config_section['side'])

    logger.info("Exir family — validating via adapter (login + buying power + RLC price band + fee)...")
    try:
        adapter = get_adapter(
            broker_code,
            username=username,
            password=password,
            config_section=config_section,
            captcha_decoder=decode_captcha,
            cache=cache,
        )
        prepared = adapter.prepare_order(isin=isin, side=side, config_section=config_section)
        logger.info(
            f"✓ Exir prepare OK: {username}@{broker_code} "
            f"{'Buy' if side == 1 else 'Sell'} {isin} "
            f"price={prepared.price:,} vol={prepared.volume:,}"
        )
        logger.info(f"\n✓✓✓ Exir warmup successful for {username}@{broker_code} ✓✓✓\n")
        return True
    except Exception as e:
        logger.error(f"❌ Exir warmup failed for {username}@{broker_code}: {e}")
        return False


def warmup_account(config_section: Dict[str, str], cache: TradingCache) -> bool:
    """
    Warm up cache for a single account.
    
    Args:
        config_section: Configuration section for the account
        cache: Cache manager instance
        
    Returns:
        True if successful, False otherwise
    """
    username = config_section['username']
    broker_code = config_section['broker']
    password = config_section['password']
    isin = config_section['isin']
    side = int(config_section['side'])
    
    logger.info(f"\n{'='*80}")
    logger.info(f"Warming up cache for {username}@{broker_code}")
    logger.info(f"{'='*80}")
    
    try:
        # Exir (Rayan HamAfza) uses a different protocol; divert to the adapter
        # (mirrors locustfile_new.py's order path). Family is data-driven from the
        # rendered config's broker_family, falling back to ephoenix for legacy
        # configs that predate it.
        from broker_adapters import resolve_family
        if resolve_family(broker_code, config_section) == "exir":
            return _warmup_exir(config_section, cache)

        # ephoenix family — endpoints are DATA-DRIVEN from the broker code (no
        # hardcoded enum gate), so a new standard ephoenix broker validates here
        # with no bot change. The ib shard etc. live in get_endpoints_for; the
        # mgmt UI already validates the code against the brokers table.
        endpoints = get_endpoints_for(broker_code)
        
        # Initialize API client with cache
        api_client = EphoenixAPIClient(
            broker_code=broker_code,
            username=username,
            password=password,
            captcha_decoder=decode_captcha,
            endpoints=endpoints,
            cache=cache
        )
        
        # Step 1: Authenticate and cache token
        logger.info("Step 1: Authenticating and caching token...")
        try:
            api_client.authenticate()
            logger.info("✓ Token cached (expires in 2 hours)")
        except Exception as e:
            logger.error(f"❌ Authentication failed for {username}@{broker_code}: {e}")
            if broker_code == 'gs':
                logger.warning("⚠️  GS broker captcha can be tricky - this account will be skipped but others will continue")
            return False
        
        # Step 2: Fetch and cache buying power
        logger.info("Step 2: Fetching and caching buying power...")
        try:
            logger.debug("  - forcing fresh buying-power fetch (cache bypass)")
            buying_power = api_client.get_buying_power(use_cache=False)  # Force fresh fetch
            logger.info(f"✓ Buying power cached: {buying_power:,.0f} Rials (expires in 5 minutes)")
        except Exception as e:
            logger.error(f"❌ Failed to fetch buying power: {e}")
            return False

        # Step 3: Fetch and cache instrument information
        logger.info("Step 3: Fetching and caching instrument information...")
        try:
            logger.debug(f"  - forcing fresh instrument-info fetch for {isin} (cache bypass)")
            instrument_info = api_client.get_instrument_info(isin, use_cache=False)  # Force fresh fetch
            logger.info(f"✓ Instrument info cached: {instrument_info['title']} ({instrument_info['symbol']})")
            logger.info(f"  - Price range: [{instrument_info['min_price']:,} - {instrument_info['max_price']:,}]")
            logger.info(f"  - Volume range: [{instrument_info['min_volume']:,} - {instrument_info['max_volume']:,}]")
            logger.info("  - Cache expires in 5 minutes")
        except Exception as e:
            logger.error(f"❌ Failed to fetch instrument info: {e}")
            return False
        
        # Step 4: Determine price and pre-calculate order parameters
        logger.info("Step 4: Pre-calculating order parameters...")
        max_volume = instrument_info['max_volume']
        if side == 1:  # Buy
            price = instrument_info['max_price']
            logger.info(f"  - Buy order - Using max price: {price:,}")
            calculated_volume = api_client.calculate_order_volume(
                isin=isin,
                side=side,
                buying_power=buying_power,
                price=price,
            )
            volume = min(calculated_volume, max_volume)
            if volume != calculated_volume:
                logger.warning(f"  ⚠ BUY volume constrained from {calculated_volume:,} to {volume:,} (max allowed)")
            else:
                logger.info(f"  ✓ BUY volume: {volume:,} shares")
        else:  # Sell
            price = instrument_info['min_price']
            logger.info(f"  - Sell order - Using min price: {price:,}")
            # Source SELL volume from real portfolio holdings — buying power
            # is meaningless for sells (issue #59). Also primes the 1h holdings
            # cache so the actual dispatch hits the cache, not the network.
            holdings = api_client.get_holdings(isin, use_cache=False)
            if holdings <= 0:
                logger.error(f"  ❌ No holdings for {isin} in {username}@{broker_code} — "
                            "skipping SELL warmup (operator likely picked the wrong ISIN)")
                return False
            volume = min(holdings, max_volume)
            capped = " (capped by max_volume per order)" if volume < holdings else ""
            logger.info(f"  ✓ SELL volume sourced from holdings={holdings:,}, "
                       f"max_volume={max_volume:,} → {volume:,}{capped}")
            # Buying power isn't used as input for SELL but the cached order
            # params row still wants a number for the column; record 0 to make
            # it obvious in downstream logs that BP wasn't the source.
            calculated_volume = volume
            buying_power = 0
        
        # Cache order parameters
        cache.save_order_params(
            username=username,
            broker_code=broker_code,
            isin=isin,
            side=side,
            price=price,
            volume=volume,
            buying_power=buying_power,
            max_allowed_volume=max_volume
        )
        logger.info("✓ Order parameters cached (expires in 5 minutes)")
        
        logger.info(f"\n✓✓✓ Cache warmup successful for {username}@{broker_code} ✓✓✓\n")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to warm up cache for {username}@{broker_code}: {e}")
        if broker_code == 'gs':
            logger.warning("⚠️  GS broker has stricter rate limiting - retry manually if needed")
        return False


def main():
    """Main entry point for cache warmup script."""
    parser = argparse.ArgumentParser(
        description='Warm up cache for stock trading bot'
    )
    parser.add_argument(
        '--config',
        type=str,
        default='config.ini',
        help='Path to configuration file (default: config.ini)'
    )
    parser.add_argument(
        '--sections',
        type=str,
        nargs='+',
        help='Specific config sections to warm up (default: all sections)'
    )
    
    args = parser.parse_args()
    
    # Load configuration
    try:
        config = configparser.ConfigParser()
        config.read(args.config)
        logger.info(f"Loaded configuration from {args.config}")
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        return
    
    # Initialize cache manager
    cache = TradingCache()
    
    # Clean expired cache entries
    logger.info("Cleaning expired cache entries...")
    expired_count = cache.clean_expired()
    logger.info(f"✓ Removed {expired_count} expired entries")
    
    # Determine which sections to process
    if args.sections:
        sections = [s for s in args.sections if s in config.sections()]
        if not sections:
            logger.error(f"No valid sections found. Available: {config.sections()}")
            return
    else:
        sections = config.sections()
    
    logger.info(f"\n{'='*80}")
    logger.info("CACHE WARMUP STARTED")
    logger.info(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Sections to process: {len(sections)}")
    logger.info(f"{'='*80}\n")
    
    # Warm up cache for each account
    success_count = 0
    failed_count = 0
    
    for section_name in sections:
        section = dict(config[section_name])
        if warmup_account(section, cache):
            success_count += 1
        else:
            failed_count += 1
    
    # Display cache statistics
    logger.info(f"\n{'='*80}")
    logger.info("CACHE WARMUP COMPLETED")
    logger.info(f"{'='*80}")
    logger.info(f"Successful accounts: {success_count}")
    logger.info(f"Failed accounts: {failed_count}")
    logger.info("\nCache Statistics:")
    
    stats = cache.get_cache_stats()
    logger.info(f"  - Total entries: {stats['total_entries']}")
    logger.info(f"  - Tokens: {stats['tokens']}")
    logger.info(f"  - Market data: {stats['market_data']}")
    logger.info(f"  - Buying power: {stats['buying_power']}")
    logger.info(f"  - Order params: {stats['order_params']}")
    logger.info(f"  - Valid entries: {stats['valid_entries']}")
    logger.info(f"  - Expired entries: {stats['expired_entries']}")
    logger.info(f"{'='*80}\n")
    
    if success_count > 0:
        logger.info("✓✓✓ Cache is ready for trading! ✓✓✓")
        logger.info("Start your Locust tests when market opens.")
    else:
        logger.warning("⚠⚠⚠ Cache warmup failed for all accounts ⚠⚠⚠")
        logger.warning("Check logs for errors and retry.")


if __name__ == '__main__':
    main()
