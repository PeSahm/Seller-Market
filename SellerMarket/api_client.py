"""
API client for ephoenix.ir stock trading platform.
Handles authentication, market data fetching, and order operations.
"""

import logging
import requests
import time
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from cache_manager import TradingCache

logger = logging.getLogger(__name__)


class EphoenixAPIClient:
    """Client for interacting with ephoenix.ir trading APIs."""
    
    def __init__(self, broker_code: str, username: str, password: str, 
                 captcha_decoder, endpoints: dict, cache: Optional[TradingCache] = None):
        """
        Initialize API client.
        
        Args:
            broker_code: Broker code (e.g., 'gs', 'bbi')
            username: Trading account username
            password: Trading account password
            captcha_decoder: Function to decode captcha images
            endpoints: Dictionary of API endpoints
            cache: TradingCache instance (creates new if None)
        """
        self.broker_code = broker_code
        self.username = username
        self.password = password
        self.captcha_decoder = captcha_decoder
        self.endpoints = endpoints
        self.token: Optional[str] = None
        self.token_expiry: Optional[datetime] = None
        self.cache = cache or TradingCache()
        
        logger.info(f"Initialized API client for broker {broker_code}, user {username}")
    
    def _save_token(self, token: str):
        """Save authentication token to cache."""
        try:
            # Save to cache manager
            if self.cache:
                self.cache.save_token(self.username, self.broker_code, token, expiry_hours=2)
            
            self.token = token
            self.token_expiry = datetime.now() + timedelta(hours=2)
            logger.info(f"Token saved for {self.username}")
        except Exception as e:
            logger.error(f"Failed to save token: {e}")
    
    def _load_token(self) -> Optional[str]:
        """Load token from cache if still valid."""
        # Try cache manager
        if self.cache:
            token = self.cache.get_token(self.username, self.broker_code)
            if token:
                self.token = token
                self.token_expiry = datetime.now() + timedelta(hours=2)
                return token
        
        logger.debug(f"No cached token found for {self.username}")
        return None
    
    def _fetch_captcha(self) -> Dict[str, str]:
        """Fetch captcha from server."""
        try:
            # GS broker needs extra delay due to stricter rate limiting
            delay = 1 if self.broker_code == 'gs' else 1
            time.sleep(delay)
            
            response = requests.get(self.endpoints['captcha'], timeout=10)
            response.raise_for_status()
            data = response.json()
            
            logger.debug(f"Fetched captcha for {self.username}@{self.broker_code}")
            return {
                'captcha_byte_data': data['captchaByteData'],
                'salt': data['salt'],
                'hashed_captcha': data['hashedCaptcha']
            }
        except Exception as e:
            logger.error(f"Failed to fetch captcha for {self.username}@{self.broker_code}: {e}")
            raise
    
    def _login_with_captcha(self) -> Optional[str]:
        """Perform login with captcha."""
        try:
            captcha_data = self._fetch_captcha()
            captcha_value = self.captcha_decoder(captcha_data['captcha_byte_data'])
            
            if not captcha_value:
                logger.warning(f"Captcha decoder returned empty value for {self.username}@{self.broker_code}")
                return None
            
            logger.debug(f"Decoded captcha: {captcha_value}")
            
            # GS broker needs extra delay between captcha fetch and login
            if self.broker_code == 'gs':
                time.sleep(2)
            
            login_data = {
                "loginName": self.username,
                "password": self.password,
                "captcha": {
                    "hash": captcha_data['hashed_captcha'],
                    "salt": captcha_data['salt'],
                    "value": captcha_value
                }
            }
            
            response = requests.post(self.endpoints['login'], json=login_data, timeout=10)
            response.raise_for_status()
            
            token = response.json().get('token')
            if token:
                logger.info(f"Login successful for {self.username}@{self.broker_code}")
                return token
            else:
                logger.warning(f"Login response missing token for {self.username}@{self.broker_code}")
                return None
                
        except Exception as e:
            logger.error(f"Login failed for {self.username}@{self.broker_code}: {e}")
            return None
    
    def authenticate(self) -> str:
        """
        Authenticate and get valid token.
        Uses cached token if available, otherwise performs login.
        
        Returns:
            Valid JWT token
        """
        # Check if we have a valid cached token
        if self.token and self.token_expiry and datetime.now() < self.token_expiry:
            logger.debug(f"Using existing token for {self.username}")
            return self.token
        
        # Try to load from file
        token = self._load_token()
        if token:
            return token
        
        # Perform login with retry
        max_retries = 100
        for attempt in range(max_retries):
            logger.info(f"Login attempt {attempt + 1}/{max_retries} for {self.username}")
            token = self._login_with_captcha()
            
            if token:
                self._save_token(token)
                return token
        
        raise Exception(f"Failed to authenticate after {max_retries} attempts")
    
    def get_buying_power(self, use_cache: bool = True) -> float:
        """
        Get current buying power for the account.
        
        Args:
            use_cache: Whether to use cached value if available
        
        Returns:
            Available buying power in Rials
        """
        # Try cache first
        if use_cache and self.cache:
            cached_bp = self.cache.get_buying_power(self.username, self.broker_code)
            if cached_bp is not None:
                return cached_bp
        
        try:
            token = self.authenticate()
            headers = {
                'authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
            response = requests.get(self.endpoints['trading_book'], headers=headers)
            response.raise_for_status()
            
            data = response.json()
            buying_power = data.get('buyingPower', 0)
            
            # Cache the buying power
            if self.cache:
                self.cache.save_buying_power(self.username, self.broker_code, buying_power)
            
            logger.info(f"Buying power for {self.username}: {buying_power:,.0f} Rials")
            return buying_power
            
        except Exception as e:
            logger.error(f"Failed to get buying power: {e}")
            raise
    
    def get_instrument_info(self, isin: str, use_cache: bool = True) -> Dict[str, Any]:
        """
        Get instrument information including price limits and max volume.
        
        Args:
            isin: Stock ISIN code
            use_cache: Whether to use cached value if available
            
        Returns:
            Dictionary with instrument and trading data
        """
        # Try cache first
        if use_cache and self.cache:
            cached_data = self.cache.get_market_data(isin)
            if cached_data is not None:
                return cached_data
        
        try:
            token = self.authenticate()
            headers = {
                'authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            data = {'isinList': [isin]}
            response = requests.post(self.endpoints['market_data'], headers=headers, json=data)
            response.raise_for_status()
            
            instruments = response.json()
            if not instruments:
                raise ValueError(f"No data found for ISIN {isin}")
            
            instrument_data = instruments[0]
            result = {
                'isin': isin,
                'symbol': instrument_data['i']['s'],
                'title': instrument_data['i']['t'],
                'max_price': instrument_data['t']['maxap'],
                'min_price': instrument_data['t']['minap'],
                'last_price': instrument_data['t']['cup'],
                'max_volume': instrument_data['i']['maxeq'],
                'min_volume': instrument_data['i']['mineq'],
            }
            
            # Cache the market data
            if self.cache:
                self.cache.save_market_data(isin, result)
            
            logger.info(f"Instrument {isin} ({result['symbol']}): "
                       f"Price range [{result['min_price']}-{result['max_price']}], "
                       f"Max volume {result['max_volume']:,}")
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to get instrument info for {isin}: {e}")
            raise
    
    def calculate_order_volume(self, isin: str, side: int, 
                              buying_power: float, price: float) -> int:
        """
        Calculate order volume based on buying power.
        
        Args:
            isin: Stock ISIN code
            side: Order side (1=Buy, 2=Sell)
            buying_power: Available buying power
            price: Order price
            
        Returns:
            Calculated volume
        """
        try:
            token = self.authenticate()
            headers = {
                'authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
            data = {
                'isin': isin,
                'side': side,
                'totalNetAmount': buying_power,
                'price': price
            }
            
            response = requests.post(self.endpoints['calculate_order'], 
                                    headers=headers, json=data)
            response.raise_for_status()
            
            result = response.json()
            volume = result.get('volume', 0)
            
            logger.info(f"Calculated volume for {isin}: {volume:,} shares "
                       f"(BP: {buying_power:,.0f}, Price: {price})")
            
            return volume
            
        except Exception as e:
            logger.error(f"Failed to calculate order volume: {e}")
            raise
    
    def place_order(self, order_data: dict) -> Dict[str, Any]:
        """
        Place a new order.
        
        Args:
            order_data: Order parameters as JSON dict
            
        Returns:
            Order response
        """
        try:
            token = self.authenticate()
            headers = {
                'authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.post(self.endpoints['order'], 
                                    headers=headers, data=order_data)
            response.raise_for_status()
            
            logger.info(f"Order placed successfully for {self.username}")
            return response.json()
            
        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            raise
    
    def get_open_orders(self) -> list:
        """
        Get list of open orders.
        
        Returns:
            List of open orders
        """
        try:
            token = self.authenticate()
            headers = {
                'authorization': f'Bearer {token}',
                'Accept': 'application/json'
            }
            
            url = f"{self.endpoints['open_orders']}?type=1"
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            
            orders = response.json()
            logger.info(f"Retrieved {len(orders)} open orders for {self.username}")
            
            return orders
            
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            raise
