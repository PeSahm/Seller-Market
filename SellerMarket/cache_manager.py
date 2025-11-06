"""
Caching system for trading bot data.
Caches tokens, market data, buying power, and calculated volumes.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict
from enum import Enum

logger = logging.getLogger(__name__)


class CacheType(Enum):
    """Types of cached data."""
    TOKEN = "token"
    MARKET_DATA = "market_data"
    BUYING_POWER = "buying_power"
    ORDER_PARAMS = "order_params"


@dataclass
class CachedToken:
    """Cached authentication token."""
    token: str
    username: str
    broker_code: str
    created_at: str
    expires_at: str
    
    def is_valid(self) -> bool:
        """Check if token is still valid."""
        expiry = datetime.fromisoformat(self.expires_at)
        return datetime.now() < expiry


@dataclass
class CachedMarketData:
    """Cached market data for a symbol."""
    isin: str
    symbol: str
    title: str
    max_price: float
    min_price: float
    last_price: float
    max_volume: int
    min_volume: int
    created_at: str
    expires_at: str
    
    def is_valid(self) -> bool:
        """Check if market data is still valid."""
        expiry = datetime.fromisoformat(self.expires_at)
        return datetime.now() < expiry


@dataclass
class CachedBuyingPower:
    """Cached buying power."""
    username: str
    broker_code: str
    buying_power: float
    created_at: str
    expires_at: str
    
    def is_valid(self) -> bool:
        """Check if buying power is still valid."""
        expiry = datetime.fromisoformat(self.expires_at)
        return datetime.now() < expiry


@dataclass
class CachedOrderParams:
    """Cached order parameters."""
    username: str
    broker_code: str
    isin: str
    side: int
    price: float
    volume: int
    buying_power: float
    max_allowed_volume: int
    created_at: str
    expires_at: str
    
    def is_valid(self) -> bool:
        """Check if order params are still valid."""
        expiry = datetime.fromisoformat(self.expires_at)
        return datetime.now() < expiry


class TradingCache:
    """
    Cache manager for trading bot data.
    
    Caches:
    - Authentication tokens (2 hour expiry)
    - Market data (5 minute expiry - refreshes near market open)
    - Buying power (1 minute expiry - can change quickly)
    - Order parameters (30 second expiry - for instant market start)
    """
    
    def __init__(self, cache_dir: str = ".cache"):
        """
        Initialize cache manager.
        
        Args:
            cache_dir: Directory to store cache files
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        logger.info(f"Cache directory: {self.cache_dir}")
    
    def _get_cache_file(self, cache_type: CacheType, key: str) -> Path:
        """Get cache file path for a specific key."""
        filename = f"{cache_type.value}_{key}.json"
        return self.cache_dir / filename
    
    def _save_cache(self, cache_type: CacheType, key: str, data: dict):
        """Save data to cache file."""
        cache_file = self._get_cache_file(cache_type, key)
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.debug(f"Saved {cache_type.value} cache: {key}")
        except Exception as e:
            logger.error(f"Failed to save cache {cache_type.value}/{key}: {e}")
    
    def _load_cache(self, cache_type: CacheType, key: str) -> Optional[dict]:
        """Load data from cache file."""
        cache_file = self._get_cache_file(cache_type, key)
        if not cache_file.exists():
            logger.debug(f"Cache not found: {cache_type.value}/{key}")
            return None
        
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            logger.debug(f"Loaded {cache_type.value} cache: {key}")
            return data
        except Exception as e:
            logger.error(f"Failed to load cache {cache_type.value}/{key}: {e}")
            return None
    
    # Token Cache
    
    def save_token(self, username: str, broker_code: str, token: str, 
                   expiry_hours: int = 2):
        """
        Save authentication token to cache.
        
        Args:
            username: Account username
            broker_code: Broker code
            token: JWT token
            expiry_hours: Token validity in hours (default 2)
        """
        now = datetime.now()
        cached_token = CachedToken(
            token=token,
            username=username,
            broker_code=broker_code,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(hours=expiry_hours)).isoformat()
        )
        
        key = f"{username}_{broker_code}"
        self._save_cache(CacheType.TOKEN, key, asdict(cached_token))
        logger.info(f"✓ Token cached for {username}@{broker_code} (expires in {expiry_hours}h)")
    
    def get_token(self, username: str, broker_code: str) -> Optional[str]:
        """
        Get cached token if still valid.
        
        Args:
            username: Account username
            broker_code: Broker code
            
        Returns:
            Token if valid, None if expired or not found
        """
        key = f"{username}_{broker_code}"
        data = self._load_cache(CacheType.TOKEN, key)
        
        if not data:
            return None
        
        cached_token = CachedToken(**data)
        if cached_token.is_valid():
            remaining = datetime.fromisoformat(cached_token.expires_at) - datetime.now()
            logger.info(f"✓ Using cached token for {username}@{broker_code} "
                       f"(valid for {remaining.seconds // 60}m)")
            return cached_token.token
        else:
            logger.info(f"⚠ Cached token expired for {username}@{broker_code}")
            return None
    
    # Market Data Cache
    
    def save_market_data(self, isin: str, market_data: Dict[str, Any], 
                        expiry_minutes: int = 5):
        """
        Save market data to cache.
        
        Args:
            isin: Stock ISIN code
            market_data: Market data dictionary
            expiry_minutes: Cache validity in minutes (default 5)
        """
        now = datetime.now()
        cached_data = CachedMarketData(
            isin=isin,
            symbol=market_data['symbol'],
            title=market_data['title'],
            max_price=market_data['max_price'],
            min_price=market_data['min_price'],
            last_price=market_data['last_price'],
            max_volume=market_data['max_volume'],
            min_volume=market_data['min_volume'],
            created_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=expiry_minutes)).isoformat()
        )
        
        self._save_cache(CacheType.MARKET_DATA, isin, asdict(cached_data))
        logger.info(f"✓ Market data cached for {isin} ({cached_data.symbol}) "
                   f"(expires in {expiry_minutes}m)")
    
    def get_market_data(self, isin: str) -> Optional[Dict[str, Any]]:
        """
        Get cached market data if still valid.
        
        Args:
            isin: Stock ISIN code
            
        Returns:
            Market data dict if valid, None if expired or not found
        """
        data = self._load_cache(CacheType.MARKET_DATA, isin)
        
        if not data:
            return None
        
        cached_data = CachedMarketData(**data)
        if cached_data.is_valid():
            remaining = datetime.fromisoformat(cached_data.expires_at) - datetime.now()
            logger.info(f"✓ Using cached market data for {isin} ({cached_data.symbol}) "
                       f"(valid for {remaining.seconds // 60}m)")
            return {
                'isin': cached_data.isin,
                'symbol': cached_data.symbol,
                'title': cached_data.title,
                'max_price': cached_data.max_price,
                'min_price': cached_data.min_price,
                'last_price': cached_data.last_price,
                'max_volume': cached_data.max_volume,
                'min_volume': cached_data.min_volume
            }
        else:
            logger.info(f"⚠ Cached market data expired for {isin}")
            return None
    
    # Buying Power Cache
    
    def save_buying_power(self, username: str, broker_code: str, 
                         buying_power: float, expiry_minutes: int = 1):
        """
        Save buying power to cache.
        
        Args:
            username: Account username
            broker_code: Broker code
            buying_power: Available buying power
            expiry_minutes: Cache validity in minutes (default 1)
        """
        now = datetime.now()
        cached_bp = CachedBuyingPower(
            username=username,
            broker_code=broker_code,
            buying_power=buying_power,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=expiry_minutes)).isoformat()
        )
        
        key = f"{username}_{broker_code}"
        self._save_cache(CacheType.BUYING_POWER, key, asdict(cached_bp))
        logger.info(f"✓ Buying power cached for {username}@{broker_code}: "
                   f"{buying_power:,.0f} (expires in {expiry_minutes}m)")
    
    def get_buying_power(self, username: str, broker_code: str) -> Optional[float]:
        """
        Get cached buying power if still valid.
        
        Args:
            username: Account username
            broker_code: Broker code
            
        Returns:
            Buying power if valid, None if expired or not found
        """
        key = f"{username}_{broker_code}"
        data = self._load_cache(CacheType.BUYING_POWER, key)
        
        if not data:
            return None
        
        cached_bp = CachedBuyingPower(**data)
        if cached_bp.is_valid():
            remaining = datetime.fromisoformat(cached_bp.expires_at) - datetime.now()
            logger.info(f"✓ Using cached buying power for {username}@{broker_code}: "
                       f"{cached_bp.buying_power:,.0f} (valid for {remaining.seconds}s)")
            return cached_bp.buying_power
        else:
            logger.info(f"⚠ Cached buying power expired for {username}@{broker_code}")
            return None
    
    # Order Parameters Cache
    
    def save_order_params(self, username: str, broker_code: str, isin: str, 
                         side: int, price: float, volume: int, buying_power: float,
                         max_allowed_volume: int, expiry_seconds: int = 30):
        """
        Save calculated order parameters to cache.
        
        Args:
            username: Account username
            broker_code: Broker code
            isin: Stock ISIN
            side: Order side (1=Buy, 2=Sell)
            price: Order price
            volume: Calculated volume
            buying_power: Buying power used
            max_allowed_volume: Maximum allowed volume
            expiry_seconds: Cache validity in seconds (default 30)
        """
        now = datetime.now()
        cached_params = CachedOrderParams(
            username=username,
            broker_code=broker_code,
            isin=isin,
            side=side,
            price=price,
            volume=volume,
            buying_power=buying_power,
            max_allowed_volume=max_allowed_volume,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=expiry_seconds)).isoformat()
        )
        
        key = f"{username}_{broker_code}_{isin}"
        self._save_cache(CacheType.ORDER_PARAMS, key, asdict(cached_params))
        logger.info(f"✓ Order params cached for {username}@{broker_code}/{isin}: "
                   f"{volume:,} @ {price:,} (expires in {expiry_seconds}s)")
    
    def get_order_params(self, username: str, broker_code: str, 
                        isin: str) -> Optional[Dict[str, Any]]:
        """
        Get cached order parameters if still valid.
        
        Args:
            username: Account username
            broker_code: Broker code
            isin: Stock ISIN
            
        Returns:
            Order params dict if valid, None if expired or not found
        """
        key = f"{username}_{broker_code}_{isin}"
        data = self._load_cache(CacheType.ORDER_PARAMS, key)
        
        if not data:
            return None
        
        cached_params = CachedOrderParams(**data)
        if cached_params.is_valid():
            remaining = datetime.fromisoformat(cached_params.expires_at) - datetime.now()
            logger.info(f"✓ Using cached order params for {username}@{broker_code}/{isin}: "
                       f"{cached_params.volume:,} @ {cached_params.price:,} "
                       f"(valid for {remaining.seconds}s)")
            return {
                'isin': cached_params.isin,
                'side': cached_params.side,
                'price': cached_params.price,
                'volume': cached_params.volume,
                'buying_power': cached_params.buying_power,
                'max_allowed_volume': cached_params.max_allowed_volume
            }
        else:
            logger.info(f"⚠ Cached order params expired for {username}@{broker_code}/{isin}")
            return None
    
    # Cache Management
    
    def clear_cache(self, cache_type: Optional[CacheType] = None, key: Optional[str] = None):
        """
        Clear cache files.
        
        Args:
            cache_type: Type of cache to clear (None = all types)
            key: Specific key to clear (None = all keys)
        """
        if cache_type and key:
            # Clear specific cache file
            cache_file = self._get_cache_file(cache_type, key)
            if cache_file.exists():
                cache_file.unlink()
                logger.info(f"✓ Cleared cache: {cache_type.value}/{key}")
        elif cache_type:
            # Clear all files of a type
            pattern = f"{cache_type.value}_*.json"
            for cache_file in self.cache_dir.glob(pattern):
                cache_file.unlink()
            logger.info(f"✓ Cleared all {cache_type.value} caches")
        else:
            # Clear all cache files
            for cache_file in self.cache_dir.glob("*.json"):
                cache_file.unlink()
            logger.info(f"✓ Cleared all caches")
    
    def clean_expired(self):
        """Remove expired cache files."""
        expired_count = 0
        
        for cache_file in self.cache_dir.glob("*.json"):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                expires_at = data.get('expires_at')
                if expires_at:
                    expiry = datetime.fromisoformat(expires_at)
                    if datetime.now() >= expiry:
                        cache_file.unlink()
                        expired_count += 1
                        logger.debug(f"Removed expired cache: {cache_file.name}")
            except Exception as e:
                logger.warning(f"Failed to check cache file {cache_file.name}: {e}")
        
        if expired_count > 0:
            logger.info(f"✓ Cleaned {expired_count} expired cache file(s)")
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        tokens = 0
        market_data = 0
        buying_power = 0
        order_params = 0
        valid_entries = 0
        expired_entries = 0
        
        now = datetime.now()
        
        # Count all cache files
        if self.cache_dir.exists():
            for file in self.cache_dir.glob('*.json'):
                try:
                    with open(file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    # Determine type from filename
                    if file.name.startswith('token_'):
                        tokens += 1
                    elif file.name.startswith('market_data_'):
                        market_data += 1
                    elif file.name.startswith('buying_power_'):
                        buying_power += 1
                    elif file.name.startswith('order_params_'):
                        order_params += 1
                    
                    # Check if valid or expired
                    expires_at_str = data.get('expires_at')
                    if expires_at_str:
                        expires_at = datetime.fromisoformat(expires_at_str)
                        if now < expires_at:
                            valid_entries += 1
                        else:
                            expired_entries += 1
                except:
                    pass
        
        total_entries = tokens + market_data + buying_power + order_params
        
        return {
            'total_entries': total_entries,
            'tokens': tokens,
            'market_data': market_data,
            'buying_power': buying_power,
            'order_params': order_params,
            'valid_entries': valid_entries,
            'expired_entries': expired_entries
        }
