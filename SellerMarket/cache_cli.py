"""
Cache Management CLI Tool

Command-line tool for managing the trading bot cache.

Commands:
    python cache_cli.py stats              - Display cache statistics
    python cache_cli.py clean              - Remove expired entries
    python cache_cli.py clear [type]       - Clear cache (all or specific type)
    python cache_cli.py show [type] [key]  - Show specific cache entry
    python cache_cli.py list [type]        - List all keys of a type

Cache Types:
    tokens      - Authentication tokens
    market      - Market data (prices, volumes)
    buying      - Buying power data
    orders      - Pre-calculated order parameters
"""

import argparse
import sys
from datetime import datetime
from typing import Optional
import json

from cache_manager import TradingCache


def format_timestamp(timestamp: float) -> str:
    """Format Unix timestamp to readable string."""
    dt = datetime.fromtimestamp(timestamp)
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def format_seconds(seconds: float) -> str:
    """Format seconds to readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f}m"
    else:
        return f"{seconds/3600:.1f}h"


def cmd_stats(cache: TradingCache) -> None:
    """Display cache statistics."""
    stats = cache.get_cache_stats()
    
    print("\n" + "="*80)
    print("CACHE STATISTICS")
    print("="*80)
    print(f"Total entries:    {stats['total_entries']}")
    print(f"Valid entries:    {stats['valid_entries']}")
    print(f"Expired entries:  {stats['expired_entries']}")
    print("\nBreakdown by type:")
    print(f"  - Tokens:       {stats['tokens']}")
    print(f"  - Market data:  {stats['market_data']}")
    print(f"  - Buying power: {stats['buying_power']}")
    print(f"  - Order params: {stats['order_params']}")
    print("="*80 + "\n")


def cmd_clean(cache: TradingCache) -> None:
    """Remove expired entries."""
    print("\nCleaning expired cache entries...")
    count = cache.clean_expired()
    print(f"✓ Removed {count} expired entries\n")


def cmd_clear(cache: TradingCache, cache_type: Optional[str] = None) -> None:
    """Clear cache."""
    if cache_type:
        print(f"\nClearing {cache_type} cache...")
    else:
        print("\nClearing all cache...")
        
    cache.clear_cache(cache_type)
    print("✓ Cache cleared\n")


def cmd_list(cache: TradingCache, cache_type: str) -> None:
    """List all cache keys of a specific type."""
    cache_dir = cache.cache_dir
    
    type_mapping = {
        'tokens': 'tokens',
        'market': 'market_data',
        'buying': 'buying_power',
        'orders': 'order_params'
    }
    
    if cache_type not in type_mapping:
        print(f"\nError: Invalid cache type '{cache_type}'")
        print("Valid types: tokens, market, buying, orders\n")
        return
    
    subdir = type_mapping[cache_type]
    cache_path = cache_dir / subdir
    
    if not cache_path.exists():
        print(f"\nNo {cache_type} cache entries found.\n")
        return
    
    files = list(cache_path.glob('*.json'))
    
    print(f"\n{cache_type.upper()} CACHE ENTRIES ({len(files)})")
    print("="*80)
    
    for file_path in sorted(files):
        key = file_path.stem
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            expires_at = data.get('expires_at', 0)
            created_at = data.get('created_at', 0)
            now = datetime.now().timestamp()
            
            is_expired = now > expires_at
            time_left = expires_at - now if not is_expired else 0
            
            status = "EXPIRED" if is_expired else f"Valid ({format_seconds(time_left)} left)"
            
            print(f"\nKey: {key}")
            print(f"  Status:     {status}")
            print(f"  Created:    {format_timestamp(created_at)}")
            print(f"  Expires:    {format_timestamp(expires_at)}")
            
        except Exception as e:
            print(f"\nKey: {key}")
            print(f"  Error: {e}")
    
    print("\n" + "="*80 + "\n")


def cmd_show(cache: TradingCache, cache_type: str, key: str) -> None:
    """Show specific cache entry."""
    type_mapping = {
        'tokens': ('tokens', cache.get_token),
        'market': ('market_data', cache.get_market_data),
        'buying': ('buying_power', cache.get_buying_power),
        'orders': ('order_params', cache.get_order_params)
    }
    
    if cache_type not in type_mapping:
        print(f"\nError: Invalid cache type '{cache_type}'")
        print("Valid types: tokens, market, buying, orders\n")
        return
    
    subdir, getter = type_mapping[cache_type]
    cache_path = cache.cache_dir / subdir / f"{key}.json"
    
    if not cache_path.exists():
        print(f"\nCache entry not found: {key}\n")
        return
    
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        expires_at = data.get('expires_at', 0)
        created_at = data.get('created_at', 0)
        now = datetime.now().timestamp()
        
        is_expired = now > expires_at
        time_left = expires_at - now if not is_expired else 0
        
        print(f"\n{cache_type.upper()} CACHE ENTRY")
        print("="*80)
        print(f"Key:        {key}")
        print(f"Status:     {'EXPIRED' if is_expired else 'Valid'}")
        print(f"Created:    {format_timestamp(created_at)}")
        print(f"Expires:    {format_timestamp(expires_at)}")
        if not is_expired:
            print(f"Time left:  {format_seconds(time_left)}")
        print("\nData:")
        print("-"*80)
        
        # Pretty print the data
        data_content = data.get('data', {})
        print(json.dumps(data_content, indent=2, ensure_ascii=False))
        
        print("="*80 + "\n")
        
    except Exception as e:
        print(f"\nError reading cache entry: {e}\n")


def main():
    """Main entry point for cache CLI."""
    parser = argparse.ArgumentParser(
        description='Cache management tool for trading bot',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s stats                    Show cache statistics
  %(prog)s clean                    Remove expired entries
  %(prog)s clear                    Clear all cache
  %(prog)s clear tokens             Clear only token cache
  %(prog)s list market              List all market data entries
  %(prog)s show tokens user1_gs     Show specific token entry
        """
    )
    
    parser.add_argument(
        'command',
        choices=['stats', 'clean', 'clear', 'list', 'show'],
        help='Command to execute'
    )
    
    parser.add_argument(
        'args',
        nargs='*',
        help='Additional arguments for the command'
    )
    
    args = parser.parse_args()
    
    # Initialize cache
    cache = TradingCache()
    
    # Execute command
    try:
        if args.command == 'stats':
            cmd_stats(cache)
            
        elif args.command == 'clean':
            cmd_clean(cache)
            
        elif args.command == 'clear':
            cache_type = args.args[0] if args.args else None
            if cache_type and cache_type not in ['tokens', 'market', 'buying', 'orders']:
                print(f"\nError: Invalid cache type '{cache_type}'")
                print("Valid types: tokens, market, buying, orders\n")
                sys.exit(1)
            cmd_clear(cache, cache_type)
            
        elif args.command == 'list':
            if not args.args:
                print("\nError: Please specify cache type to list")
                print("Usage: cache_cli.py list <type>")
                print("Valid types: tokens, market, buying, orders\n")
                sys.exit(1)
            cmd_list(cache, args.args[0])
            
        elif args.command == 'show':
            if len(args.args) < 2:
                print("\nError: Please specify cache type and key")
                print("Usage: cache_cli.py show <type> <key>")
                print("Valid types: tokens, market, buying, orders\n")
                sys.exit(1)
            cmd_show(cache, args.args[0], args.args[1])
            
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.\n")
        sys.exit(0)
    except Exception as e:
        print(f"\nError: {e}\n")
        sys.exit(1)


if __name__ == '__main__':
    main()
