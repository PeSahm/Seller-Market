"""
Portfolio Manager for Auto-Selling Stocks Based on Market Conditions.

This module monitors owned stocks and automatically sells them when specific
market conditions are met.

## Market Timing (Tehran Stock Exchange):

| Time Period       | Description                                    | Actions Allowed    |
|-------------------|------------------------------------------------|--------------------|
| 08:45:00-08:55:00 | Pre-market order entry                         | Place/Cancel/Modify|
| 08:55:00-09:02:00 | Order FREEZE - No modifications allowed        | None               |
| 09:00:00          | Market opens, orders start matching            | None               |
| 09:02:00+         | Trading session, can modify orders             | Place/Cancel/Modify|

## Sell Logic:

1. **High Demand (Queued/Seller's Market)**:
   - If buy volume at best price < threshold: Stock is losing demand
   - Sell quickly at best BUY price to get maximum profit before queue breaks

2. **Normal Market (Not Queued)**:
   - Competition with other sellers
   - Sell at last_price * sell_discount (e.g., 0.99 = -1%)
   - Continuously monitor and revise orders as price moves
   - Must be aggressive to compete with other sellers

3. **Order Freeze Handling**:
   - During 08:55:00-09:02:00, cannot cancel/modify orders
   - Wait until 09:02:00 to analyze and adjust
   - System tracks this and queues actions for after freeze

4. **Partial Fills**:
   - Track executed vs remaining volume
   - Recalculate and resubmit for remaining shares
   - Goal: Sell ALL shares

Usage:
    python portfolio_manager.py --config portfolio_config.ini
"""

import argparse
import configparser
import logging
import threading
import time
import json
import os
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import IntEnum

from broker_enum import BrokerCode
from api_client import EphoenixAPIClient
from cache_manager import TradingCache
from captcha_utils import decode_captcha

# =============================================================================
# Market Timing Constants (Tehran Stock Exchange)
# =============================================================================

# Pre-market order entry period
PREMARKET_START = datetime.strptime("08:45:00", "%H:%M:%S").time()
PREMARKET_END = datetime.strptime("08:55:00", "%H:%M:%S").time()

# Order freeze period - NO modifications allowed
ORDER_FREEZE_START = datetime.strptime("08:55:00", "%H:%M:%S").time()
ORDER_FREEZE_END = datetime.strptime("09:02:00", "%H:%M:%S").time()

# Market open time (orders start matching)
MARKET_OPEN = datetime.strptime("09:00:00", "%H:%M:%S").time()

# Trading session
TRADING_START = datetime.strptime("09:02:00", "%H:%M:%S").time()
TRADING_END = datetime.strptime("12:30:00", "%H:%M:%S").time()


class MarketPhase(IntEnum):
    """Current market phase based on time."""
    CLOSED = 0           # Outside trading hours
    PREMARKET = 1        # 08:45:00 - 08:55:00: Can place/cancel orders
    ORDER_FREEZE = 2     # 08:55:00 - 09:02:00: NO modifications allowed
    TRADING = 3          # 09:02:00 - 12:30:00: Normal trading


def get_market_phase() -> MarketPhase:
    """Determine current market phase based on time."""
    now = datetime.now().time()
    
    if PREMARKET_START <= now < ORDER_FREEZE_START:
        return MarketPhase.PREMARKET
    elif ORDER_FREEZE_START <= now < ORDER_FREEZE_END:
        return MarketPhase.ORDER_FREEZE
    elif TRADING_START <= now <= TRADING_END:
        return MarketPhase.TRADING
    else:
        return MarketPhase.CLOSED


def can_modify_orders() -> bool:
    """Check if we can modify/cancel orders right now."""
    phase = get_market_phase()
    return phase in (MarketPhase.PREMARKET, MarketPhase.TRADING)


def seconds_until_can_modify() -> float:
    """Get seconds until we can modify orders again (0 if already can)."""
    if can_modify_orders():
        return 0.0
    
    now = datetime.now()
    freeze_end = now.replace(
        hour=ORDER_FREEZE_END.hour,
        minute=ORDER_FREEZE_END.minute,
        second=ORDER_FREEZE_END.second,
        microsecond=0
    )
    
    if now.time() < ORDER_FREEZE_END:
        return (freeze_end - now).total_seconds()
    
    # After trading hours, return until next day premarket
    return 0.0

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('portfolio_manager.log', mode='a', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


class OrderState(IntEnum):
    """Order states from broker API."""
    ADDING = 1            # Sending order
    ADDED = 2             # Registered in core
    EXECUTED = 3          # Fully executed
    CANCELED = 4          # Canceled
    ERROR = 5             # Order error
    PARTIAL_EXECUTED = 6  # Partially executed
    MODIFIED = 7          # Modified
    CANCELING = 8         # Cancellation requested
    MODIFYING = 9         # Modification requested
    SLE_ERROR = 10        # SLE error
    DEPRECATED = 11       # Was executed (deprecated)


@dataclass
class PortfolioPosition:
    """Represents a stock position in the portfolio."""
    isin: str
    symbol: str
    quantity: int
    average_price: float
    current_price: float = 0.0
    
    @property
    def market_value(self) -> float:
        """Calculate current market value."""
        return self.quantity * self.current_price


@dataclass
class BestLimit:
    """Represents a single row from the best limits table."""
    isin: str
    row: int  # Row number (1-6)
    buy_volume: int  # bv
    buy_order_count: int  # boc
    buy_price: float  # bp
    sell_volume: int  # sv
    sell_order_count: int  # soc
    sell_price: float  # sp


@dataclass
class MarketCondition:
    """Represents current market condition for a stock."""
    isin: str
    symbol: str
    last_price: float  # cup - current price
    max_price: float  # maxap
    min_price: float  # minap
    best_limits: List[BestLimit]
    total_buy_volume: int
    total_sell_volume: int
    is_queued: bool  # True if in queue (Seller's Market)
    timestamp: datetime = field(default_factory=datetime.now)
    
    @property
    def best_buy_price(self) -> float:
        """Get best buy price from first row."""
        if self.best_limits and self.best_limits[0].buy_price > 0:
            return self.best_limits[0].buy_price
        return 0.0
    
    @property
    def best_sell_price(self) -> float:
        """Get best sell price from first row."""
        if self.best_limits and self.best_limits[0].sell_price > 0:
            return self.best_limits[0].sell_price
        return 0.0
    
    @property
    def first_row_buy_volume(self) -> int:
        """Get buy volume at best price (first row)."""
        if self.best_limits:
            return self.best_limits[0].buy_volume
        return 0


@dataclass
class SellOrder:
    """Represents a sell order to be placed or tracked."""
    isin: str
    price: float
    volume: int
    serial_number: Optional[int] = None
    state: Optional[OrderState] = None
    executed_volume: int = 0
    remaining_volume: int = 0
    
    def __post_init__(self):
        self.remaining_volume = self.volume - self.executed_volume


@dataclass
class PortfolioConfig:
    """Configuration for portfolio watching."""
    section_name: str
    username: str
    password: str
    broker: str
    isin: str
    min_buy_volume: int = 30_000_000  # Default 30 million
    sell_discount: float = 0.99  # Default 99% of last price
    check_interval: float = 1.0  # Seconds between checks
    
    @classmethod
    def from_config_section(cls, section_name: str, section: Dict[str, str]) -> 'PortfolioConfig':
        """Create config from INI section."""
        return cls(
            section_name=section_name,
            username=section.get('username', ''),
            password=section.get('password', ''),
            broker=section.get('broker', 'bbi'),
            isin=section.get('isin', ''),
            min_buy_volume=int(section.get('min_buy_volume', 30_000_000)),
            sell_discount=float(section.get('sell_discount', 0.99)),
            check_interval=float(section.get('check_interval', 1.0))
        )


class TelegramNotifier:
    """Sends notifications to Telegram."""
    
    def __init__(self):
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.user_id = os.getenv('TELEGRAM_USER_ID') or os.getenv('USER_ID')
    
    def send(self, message: str) -> bool:
        """Send a notification to Telegram."""
        if not self.bot_token or not self.user_id:
            logger.warning("Telegram credentials not configured")
            return False
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                'chat_id': self.user_id,
                'text': message,
                'parse_mode': 'Markdown'
            }
            response = requests.post(url, json=payload, timeout=10)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")
            return False


class PortfolioAPIClient(EphoenixAPIClient):
    """Extended API client with portfolio management methods."""
    
    def get_portfolio_positions(self) -> List[PortfolioPosition]:
        """
        Get current portfolio positions.
        
        Returns:
            List of PortfolioPosition objects
        """
        try:
            token = self.authenticate()
            headers = {
                'authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'x-sessionId': f'OMS{token[:36]}'  # Session ID derived from token
            }
            
            response = requests.post(
                self.endpoints['portfolio'],
                headers=headers,
                json={'entity': True},
                timeout=10
            )
            response.raise_for_status()
            
            positions = []
            data = response.json()
            
            # Parse portfolio response
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict) and 'isin' in item:
                    positions.append(PortfolioPosition(
                        isin=item.get('isin', ''),
                        symbol=item.get('symbol', ''),
                        quantity=int(item.get('quantity', 0)),
                        average_price=float(item.get('averagePrice', 0))
                    ))
            
            logger.info(f"Retrieved {len(positions)} portfolio positions")
            return positions
            
        except Exception as e:
            logger.error(f"Failed to get portfolio positions: {e}")
            raise
    
    def get_best_limits(self, isin: str) -> MarketCondition:
        """
        Get best limits (order book) for a stock.
        
        Args:
            isin: Stock ISIN code
            
        Returns:
            MarketCondition object with best limits data
        """
        try:
            token = self.authenticate()
            headers = {
                'authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.post(
                self.endpoints['market_data'],
                headers=headers,
                json={'isinList': [isin]},
                timeout=10
            )
            response.raise_for_status()
            
            data = response.json()
            if not data:
                raise ValueError(f"No market data for ISIN {isin}")
            
            instrument = data[0]
            i_data = instrument.get('i', {})
            t_data = instrument.get('t', {})
            bl_data = instrument.get('bl', [])
            
            # Parse best limits
            best_limits = []
            total_buy_volume = 0
            total_sell_volume = 0
            
            for bl in bl_data:
                buy_volume = int(bl.get('bv', 0))
                sell_volume = int(bl.get('sv', 0))
                total_buy_volume += buy_volume
                total_sell_volume += sell_volume
                
                best_limits.append(BestLimit(
                    isin=isin,
                    row=int(bl.get('r', 0)),
                    buy_volume=buy_volume,
                    buy_order_count=int(bl.get('boc', 0)),
                    buy_price=float(bl.get('bp', 0)),
                    sell_volume=sell_volume,
                    sell_order_count=int(bl.get('soc', 0)),
                    sell_price=float(bl.get('sp', 0))
                ))
            
            # Sort by row number
            best_limits.sort(key=lambda x: x.row)
            
            # Determine if queued (Seller's Market)
            # Queued = all sell volume is at max price OR no sell orders
            max_price = float(t_data.get('maxap', 0))
            is_queued = total_sell_volume == 0 or (
                best_limits and 
                best_limits[0].sell_price >= max_price and
                best_limits[0].sell_volume > 0
            )
            
            return MarketCondition(
                isin=isin,
                symbol=i_data.get('s', ''),
                last_price=float(t_data.get('cup', 0)),
                max_price=max_price,
                min_price=float(t_data.get('minap', 0)),
                best_limits=best_limits,
                total_buy_volume=total_buy_volume,
                total_sell_volume=total_sell_volume,
                is_queued=is_queued
            )
            
        except Exception as e:
            logger.error(f"Failed to get best limits for {isin}: {e}")
            raise
    
    def cancel_order(self, serial_number: int) -> bool:
        """
        Cancel an open order.
        
        Args:
            serial_number: Order serial number
            
        Returns:
            True if cancelled successfully
        """
        try:
            token = self.authenticate()
            headers = {
                'authorization': f'Bearer {token}',
                'Accept': 'text/plain',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            url = f"{self.endpoints['cancel_order']}?serialNumber={serial_number}"
            response = requests.delete(url, headers=headers, timeout=10)
            response.raise_for_status()
            
            logger.info(f"Order {serial_number} cancelled successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to cancel order {serial_number}: {e}")
            return False
    
    def place_sell_order(self, isin: str, price: float, volume: int) -> Optional[Dict]:
        """
        Place a sell order.
        
        Args:
            isin: Stock ISIN code
            price: Sell price
            volume: Number of shares
            
        Returns:
            Order response dict or None if failed
        """
        try:
            token = self.authenticate()
            headers = {
                'authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            order_data = {
                'isin': isin,
                'side': 2,  # Sell
                'validity': 1,  # Day order
                'accountType': 1,
                'price': price,
                'volume': volume,
                'validityDate': None,
                'serialNumber': 0
            }
            
            response = requests.post(
                self.endpoints['order'],
                headers=headers,
                json=order_data,
                timeout=10
            )
            response.raise_for_status()
            
            result = response.json()
            logger.info(f"Sell order placed: {volume} shares of {isin} at {price}")
            return result
            
        except Exception as e:
            logger.error(f"Failed to place sell order: {e}")
            return None
    
    def get_order_status(self, serial_number: int) -> Optional[Dict]:
        """
        Get status of a specific order from open orders.
        
        Args:
            serial_number: Order serial number
            
        Returns:
            Order dict or None if not found
        """
        try:
            orders = self.get_open_orders()
            for order in orders:
                if order.get('serialNumber') == serial_number:
                    return order
            return None
        except Exception as e:
            logger.error(f"Failed to get order status: {e}")
            return None


class PortfolioWatcher:
    """
    Watches a portfolio position and executes sell orders based on conditions.
    
    Each PortfolioWatcher runs in its own thread and monitors a single
    account/ISIN combination.
    """
    
    def __init__(self, config: PortfolioConfig, cache: TradingCache,
                 notifier: Optional[TelegramNotifier] = None):
        """
        Initialize portfolio watcher.
        
        Args:
            config: Portfolio configuration
            cache: Cache manager instance
            notifier: Optional Telegram notifier
        """
        self.config = config
        self.cache = cache
        self.notifier = notifier or TelegramNotifier()
        
        # Validate broker
        if not BrokerCode.is_valid(config.broker):
            raise ValueError(f"Invalid broker code: {config.broker}")
        
        # Get broker endpoints
        broker_enum = BrokerCode(config.broker)
        self.endpoints = broker_enum.get_endpoints()
        
        # Initialize API client
        self.api_client = PortfolioAPIClient(
            broker_code=config.broker,
            username=config.username,
            password=config.password,
            captcha_decoder=decode_captcha,
            endpoints=self.endpoints,
            cache=cache
        )
        
        # State
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._pending_orders: List[SellOrder] = []
        self._total_sold = 0
        self._target_quantity = 0
        self._last_market_phase: Optional[MarketPhase] = None
        self._freeze_notified = False  # Track if we've notified about freeze
        
        logger.info(f"PortfolioWatcher initialized for {config.username}@{config.broker} - {config.isin}")
    
    def start(self):
        """Start watching the portfolio in a background thread."""
        if self._running:
            logger.warning("Watcher already running")
            return
        
        self._running = True
        self._freeze_notified = False
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()
        
        phase = get_market_phase()
        self.notifier.send(
            f"🔍 *Started watching* {self.config.isin}\n"
            f"Account: `{self.config.username}@{self.config.broker}`\n"
            f"Min buy volume: {self.config.min_buy_volume:,}\n"
            f"Sell discount: {self.config.sell_discount:.1%}\n"
            f"Current phase: {phase.name}"
        )
        logger.info(f"Started watching {self.config.isin}")
    
    def stop(self):
        """Stop watching the portfolio."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info(f"Stopped watching {self.config.isin}")
    
    @property
    def is_running(self) -> bool:
        """Check if watcher is running."""
        return self._running
    
    def _watch_loop(self):
        """Main watching loop."""
        while self._running:
            try:
                self._check_and_act()
            except Exception as e:
                logger.error(f"Error in watch loop: {e}")
                self.notifier.send(f"⚠️ *Error* watching {self.config.isin}: {e}")
            
            time.sleep(self.config.check_interval)
    
    def _check_and_act(self):
        """Check market conditions and take action if needed."""
        phase = get_market_phase()
        
        # Notify on phase change
        if self._last_market_phase != phase:
            self._last_market_phase = phase
            self._freeze_notified = False
            if phase != MarketPhase.CLOSED:
                self.notifier.send(f"⏰ *Market phase changed*: {phase.name}")
        
        # Skip if market is closed
        if phase == MarketPhase.CLOSED:
            logger.debug("Market closed, skipping check")
            return
        
        # Get current position from portfolio
        positions = self.api_client.get_portfolio_positions()
        position = next(
            (p for p in positions if p.isin == self.config.isin),
            None
        )
        
        if not position or position.quantity <= 0:
            if self._target_quantity > 0 and self._total_sold >= self._target_quantity:
                self.notifier.send(
                    f"✅ *All shares sold* for {self.config.isin}\n"
                    f"Total sold: {self._total_sold:,} shares"
                )
                self._running = False
            return
        
        # Update target quantity if not set
        if self._target_quantity == 0:
            self._target_quantity = position.quantity
            self.notifier.send(
                f"📊 *Tracking position* {self.config.isin}\n"
                f"Quantity: {self._target_quantity:,} shares"
            )
            logger.info(f"Target quantity set to {self._target_quantity} for {self.config.isin}")
        
        # Get market conditions (best limits)
        market = self.api_client.get_best_limits(self.config.isin)
        
        # Update pending orders status
        self._update_pending_orders()
        
        # Handle order freeze period - can only monitor, not act
        if phase == MarketPhase.ORDER_FREEZE:
            if not self._freeze_notified:
                wait_time = seconds_until_can_modify()
                self.notifier.send(
                    f"🔒 *Order freeze* until 09:02:00\n"
                    f"Position: {position.quantity:,} shares\n"
                    f"Pending orders: {len(self._pending_orders)}\n"
                    f"Wait time: {wait_time:.0f}s"
                )
                self._freeze_notified = True
            logger.debug(f"Order freeze period - waiting until 09:02:00")
            return
        
        # Determine if we need to cancel and resubmit orders at new price
        self._handle_order_repricing(market, phase)
        
        # Determine sell action
        sell_price, sell_reason = self._determine_sell_action(market, phase)
        
        if sell_price is None:
            return
        
        # Calculate remaining quantity to sell (position - pending orders)
        remaining = position.quantity - self._get_pending_volume()
        
        if remaining <= 0:
            return
        
        # Execute sell orders
        self._execute_sell(market, sell_price, remaining, sell_reason)
    
    def _handle_order_repricing(self, market: MarketCondition, phase: MarketPhase):
        """
        Check if pending orders need to be cancelled and repriced.
        
        In a fast-moving normal market, other sellers may undercut our price.
        We need to revise orders quickly to stay competitive.
        """
        if not self._pending_orders or not can_modify_orders():
            return
        
        # Only reprice in normal market (not queued)
        if market.is_queued:
            return
        
        # Calculate target price for normal market
        target_price = market.last_price * self.config.sell_discount
        
        # Check each pending order
        orders_to_cancel = []
        for order in self._pending_orders:
            if order.state in (OrderState.ADDED, OrderState.ADDING):
                # If our price is higher than target, we need to lower it
                if order.price > target_price + 1:  # +1 for rounding tolerance
                    orders_to_cancel.append(order)
                    logger.info(f"Order {order.serial_number} price {order.price} > target {target_price}, will cancel")
        
        # Cancel orders that need repricing
        for order in orders_to_cancel:
            if order.serial_number:
                success = self.api_client.cancel_order(order.serial_number)
                if success:
                    self._pending_orders.remove(order)
                    self.notifier.send(
                        f"🔄 *Repricing order* {self.config.isin}\n"
                        f"Old price: {order.price:,.0f}\n"
                        f"New target: {target_price:,.0f}"
                    )
    
    def _determine_sell_action(self, market: MarketCondition, phase: MarketPhase) -> tuple:
        """
        Determine if we should sell and at what price.
        
        Logic:
        1. Queued market + low buy volume = Demand dropping, sell at best BUY price
        2. Normal market = Competition, sell at last_price * discount
        3. Pre-market normal = Rare opportunity, sell at discounted price
        
        Returns:
            (price, reason) tuple, or (None, None) if no action needed
        """
        # Case 1: Stock is in queue (Seller's Market / high demand)
        if market.is_queued:
            # Check if demand is dropping below threshold
            if market.first_row_buy_volume < self.config.min_buy_volume:
                # Demand is low - sell at best buy price for maximum profit
                # This happens when queue is about to break
                price = market.best_buy_price
                if price > 0:
                    logger.info(f"Queue demand low ({market.first_row_buy_volume:,} < {self.config.min_buy_volume:,}), selling at best buy {price}")
                    return (price, "queue_demand_low")
            else:
                # Still high demand - hold position
                logger.debug(f"Queue demand high ({market.first_row_buy_volume:,}), holding")
                return (None, None)
        
        # Case 2: Stock is normal (not queued) - competitive selling
        else:
            # Calculate competitive sell price
            # Use discount to be competitive with other sellers
            price = market.last_price * self.config.sell_discount
            
            # Make sure price is within allowed range
            if price < market.min_price:
                price = market.min_price
            
            # Pre-market normal is urgent
            if phase == MarketPhase.PREMARKET:
                logger.info(f"Pre-market normal detected, selling at {price}")
                return (price, "premarket_normal")
            
            # Trading session normal
            logger.info(f"Normal market, selling at {price} (last={market.last_price}, discount={self.config.sell_discount})")
            return (price, "normal_market_sell")
    
    def _execute_sell(self, market: MarketCondition, price: float, 
                      quantity: int, reason: str):
        """
        Execute sell orders, splitting by max volume if needed.
        
        Args:
            market: Current market condition
            price: Sell price
            quantity: Total quantity to sell
            reason: Reason for selling
        """
        # Get max allowed volume per order
        instrument_info = self.api_client.get_instrument_info(self.config.isin)
        max_volume = instrument_info.get('max_volume', 400000)
        
        # Calculate number of orders needed
        orders_needed = (quantity + max_volume - 1) // max_volume
        
        logger.info(f"Executing sell: {quantity} shares at {price}, split into {orders_needed} orders")
        self.notifier.send(
            f"📤 *Selling* {self.config.isin}\n"
            f"Reason: {reason}\n"
            f"Price: {price:,.0f}\n"
            f"Quantity: {quantity:,}\n"
            f"Orders: {orders_needed}"
        )
        
        remaining = quantity
        for i in range(orders_needed):
            order_volume = min(remaining, max_volume)
            
            result = self.api_client.place_sell_order(
                isin=self.config.isin,
                price=price,
                volume=order_volume
            )
            
            if result:
                serial = result.get('serialNumber')
                self._pending_orders.append(SellOrder(
                    isin=self.config.isin,
                    price=price,
                    volume=order_volume,
                    serial_number=serial,
                    state=OrderState.ADDING
                ))
                logger.info(f"Order {i+1}/{orders_needed} placed: {order_volume} shares, serial {serial}")
            else:
                self.notifier.send(f"❌ *Failed* to place order {i+1}/{orders_needed}")
            
            remaining -= order_volume
    
    def _update_pending_orders(self):
        """Update status of pending orders and handle completed/cancelled ones."""
        updated_orders = []
        
        for order in self._pending_orders:
            if order.serial_number is None:
                continue
            
            status = self.api_client.get_order_status(order.serial_number)
            
            if status is None:
                # Order not in open orders - check if executed
                order.state = OrderState.EXECUTED
                self._total_sold += order.volume
                logger.info(f"Order {order.serial_number} completed: {order.volume} shares")
                continue
            
            order.state = OrderState(status.get('orderState', 1))
            order.executed_volume = int(status.get('executedVolume', 0))
            order.remaining_volume = order.volume - order.executed_volume
            
            if order.state == OrderState.EXECUTED:
                self._total_sold += order.volume
            elif order.state == OrderState.PARTIAL_EXECUTED:
                self._total_sold += order.executed_volume
                # Keep tracking remaining
                updated_orders.append(order)
            elif order.state in (OrderState.ADDED, OrderState.ADDING, OrderState.MODIFYING):
                updated_orders.append(order)
            elif order.state == OrderState.CANCELED:
                logger.info(f"Order {order.serial_number} was cancelled")
            elif order.state == OrderState.ERROR:
                logger.error(f"Order {order.serial_number} has error")
                self.notifier.send(f"❌ *Order error* {order.serial_number}")
        
        self._pending_orders = updated_orders
    
    def _get_pending_volume(self) -> int:
        """Get total volume in pending orders."""
        return sum(o.remaining_volume for o in self._pending_orders)
    
    def cancel_all_pending(self):
        """Cancel all pending orders."""
        for order in self._pending_orders:
            if order.serial_number:
                self.api_client.cancel_order(order.serial_number)
        self._pending_orders = []


class PortfolioManager:
    """
    Manages multiple PortfolioWatchers for different accounts/ISINs.
    """
    
    def __init__(self, config_file: str = 'portfolio_config.ini'):
        """
        Initialize portfolio manager.
        
        Args:
            config_file: Path to configuration file
        """
        self.config_file = config_file
        self.cache = TradingCache()
        self.notifier = TelegramNotifier()
        self.watchers: Dict[str, PortfolioWatcher] = {}
        
        logger.info(f"PortfolioManager initialized with config: {config_file}")
    
    def load_configs(self) -> List[PortfolioConfig]:
        """Load all configurations from the config file."""
        config = configparser.ConfigParser()
        config.read(self.config_file)
        
        configs = []
        for section_name in config.sections():
            section = dict(config[section_name])
            configs.append(PortfolioConfig.from_config_section(section_name, section))
        
        logger.info(f"Loaded {len(configs)} portfolio configurations")
        return configs
    
    def start_all(self):
        """Start watchers for all configured portfolios."""
        configs = self.load_configs()
        
        for cfg in configs:
            key = f"{cfg.username}@{cfg.broker}:{cfg.isin}"
            if key in self.watchers:
                logger.warning(f"Watcher already exists for {key}")
                continue
            
            watcher = PortfolioWatcher(cfg, self.cache, self.notifier)
            self.watchers[key] = watcher
            watcher.start()
        
        logger.info(f"Started {len(self.watchers)} portfolio watchers")
    
    def start_one(self, section_name: str):
        """Start watcher for a specific configuration section."""
        config = configparser.ConfigParser()
        config.read(self.config_file)
        
        if section_name not in config.sections():
            raise ValueError(f"Section {section_name} not found in config")
        
        section = dict(config[section_name])
        cfg = PortfolioConfig.from_config_section(section_name, section)
        
        key = f"{cfg.username}@{cfg.broker}:{cfg.isin}"
        if key in self.watchers and self.watchers[key].is_running:
            logger.warning(f"Watcher already running for {key}")
            return
        
        watcher = PortfolioWatcher(cfg, self.cache, self.notifier)
        self.watchers[key] = watcher
        watcher.start()
    
    def stop_all(self):
        """Stop all watchers."""
        for key, watcher in self.watchers.items():
            watcher.stop()
        self.watchers.clear()
        logger.info("All portfolio watchers stopped")
    
    def stop_one(self, section_name: str):
        """Stop watcher for a specific configuration."""
        config = configparser.ConfigParser()
        config.read(self.config_file)
        
        if section_name not in config.sections():
            return
        
        section = dict(config[section_name])
        cfg = PortfolioConfig.from_config_section(section_name, section)
        
        key = f"{cfg.username}@{cfg.broker}:{cfg.isin}"
        if key in self.watchers:
            self.watchers[key].stop()
            del self.watchers[key]
    
    def get_status(self) -> Dict[str, Any]:
        """Get status of all watchers."""
        return {
            key: {
                'running': watcher.is_running,
                'isin': watcher.config.isin,
                'account': f"{watcher.config.username}@{watcher.config.broker}",
                'total_sold': watcher._total_sold,
                'pending_orders': len(watcher._pending_orders)
            }
            for key, watcher in self.watchers.items()
        }
    
    def get_all_positions(self) -> List[Dict[str, Any]]:
        """Get current positions from all watched accounts."""
        positions = []
        seen_accounts = set()
        
        for watcher in self.watchers.values():
            account_key = f"{watcher.config.username}@{watcher.config.broker}"
            if account_key in seen_accounts:
                continue
            seen_accounts.add(account_key)
            
            try:
                account_positions = watcher.api_client.get_portfolio_positions()
                for pos in account_positions:
                    positions.append({
                        'account': account_key,
                        'isin': pos.isin,
                        'symbol': pos.symbol,
                        'quantity': pos.quantity,
                        'average_price': pos.average_price
                    })
            except Exception as e:
                logger.error(f"Failed to get positions for {account_key}: {e}")
        
        return positions


def main():
    """Main entry point for portfolio manager."""
    parser = argparse.ArgumentParser(
        description='Portfolio Manager for Auto-Selling Stocks'
    )
    parser.add_argument(
        '--config',
        type=str,
        default='portfolio_config.ini',
        help='Path to configuration file (default: portfolio_config.ini)'
    )
    parser.add_argument(
        '--section',
        type=str,
        help='Specific config section to run (default: all sections)'
    )
    
    args = parser.parse_args()
    
    manager = PortfolioManager(args.config)
    
    try:
        if args.section:
            manager.start_one(args.section)
        else:
            manager.start_all()
        
        # Keep running until interrupted
        logger.info("Portfolio manager running. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        manager.stop_all()


if __name__ == '__main__':
    main()
