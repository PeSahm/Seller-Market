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
from typing import List, Dict, Any
from datetime import datetime

from broker_enum import BrokerCode
from api_client import EphoenixAPIClient
from cache_manager import TradingCache

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


def decode_captcha(im: str) -> str:
    """
    Decode captcha image using OCR service.
    
    Args:
        im: Base64 encoded image
        
    Returns:
        Decoded captcha text
    """
    import requests
    url = 'http://localhost:8080/ocr/captcha-easy-base64'
    headers = {
        'accept': 'text/plain',
        'Content-Type': 'application/json'
    }
    data = {"base64": im}
    
    try:
        response = requests.post(url, headers=headers, json=data)
        result = response.text
        return "".join(result)
    except requests.RequestException as e:
        logger.error(f"Captcha decoding failed: {e}")
        return ""


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
        # Validate broker code
        if not BrokerCode.is_valid(broker_code):
            logger.error(f"Invalid broker code: {broker_code}")
            return False
        
        # Get broker endpoints
        broker_enum = BrokerCode(broker_code)
        endpoints = broker_enum.get_endpoints()
        
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
            token = api_client.authenticate()
            logger.info("✓ Token cached (expires in 2 hours)")
        except Exception as e:
            logger.error(f"❌ Authentication failed for {username}@{broker_code}: {e}")
            if broker_code == 'gs':
                logger.warning(f"⚠️  GS broker captcha can be tricky - this account will be skipped but others will continue")
            return False
        
        # Step 2: Fetch and cache buying power
        logger.info("Step 2: Fetching and caching buying power...")
        try:
            buying_power = api_client.get_buying_power(use_cache=False)  # Force fresh fetch
            logger.info(f"✓ Buying power cached: {buying_power:,.0f} Rials (expires in 2 hours)")
        except Exception as e:
            logger.error(f"❌ Failed to fetch buying power: {e}")
            return False
        
        # Step 3: Fetch and cache instrument information
        logger.info("Step 3: Fetching and caching instrument information...")
        try:
            instrument_info = api_client.get_instrument_info(isin, use_cache=False)  # Force fresh fetch
            logger.info(f"✓ Instrument info cached: {instrument_info['title']} ({instrument_info['symbol']})")
            logger.info(f"  - Price range: [{instrument_info['min_price']:,} - {instrument_info['max_price']:,}]")
            logger.info(f"  - Volume range: [{instrument_info['min_volume']:,} - {instrument_info['max_volume']:,}]")
            logger.info(f"  - Cache expires in 2 hours")
        except Exception as e:
            logger.error(f"❌ Failed to fetch instrument info: {e}")
            return False
        
        # Step 4: Determine price and pre-calculate order parameters
        logger.info("Step 4: Pre-calculating order parameters...")
        if side == 1:  # Buy
            price = instrument_info['max_price']
            logger.info(f"  - Buy order - Using max price: {price:,}")
        else:  # Sell
            price = instrument_info['min_price']
            logger.info(f"  - Sell order - Using min price: {price:,}")
        
        # Calculate volume
        calculated_volume = api_client.calculate_order_volume(
            isin=isin,
            side=side,
            buying_power=buying_power,
            price=price
        )
        
        # Constrain by max allowed volume
        max_volume = instrument_info['max_volume']
        volume = min(calculated_volume, max_volume)
        
        if volume != calculated_volume:
            logger.warning(f"  ⚠ Volume constrained from {calculated_volume:,} to {volume:,} (max allowed)")
        else:
            logger.info(f"  ✓ Calculated volume: {volume:,} shares")
        
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
        logger.info(f"✓ Order parameters cached (expires in 2 hours)")
        
        logger.info(f"\n✓✓✓ Cache warmup successful for {username}@{broker_code} ✓✓✓\n")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to warm up cache for {username}@{broker_code}: {e}")
        if broker_code == 'gs':
            logger.warning(f"⚠️  GS broker has stricter rate limiting - retry manually if needed")
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
    logger.info(f"CACHE WARMUP STARTED")
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
    logger.info(f"CACHE WARMUP COMPLETED")
    logger.info(f"{'='*80}")
    logger.info(f"Successful accounts: {success_count}")
    logger.info(f"Failed accounts: {failed_count}")
    logger.info(f"\nCache Statistics:")
    
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
