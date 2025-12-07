"""
Comprehensive tests for Portfolio Manager module.

Tests cover:
- PortfolioConfig parsing
- OrderState enum
- MarketPhase and timing functions
- BestLimit and MarketCondition dataclasses
- PortfolioWatcher sell decision logic
- PortfolioAPIClient API interactions (mocked)
- PortfolioManager multi-watcher management
- Volume splitting for large orders
- Order state tracking
"""

import pytest
import json
import os
from unittest.mock import Mock, MagicMock, patch, PropertyMock
from datetime import datetime, timedelta
from dataclasses import asdict

from portfolio_manager import (
    OrderState,
    MarketPhase,
    PortfolioPosition,
    BestLimit,
    MarketCondition,
    SellOrder,
    PortfolioConfig,
    TelegramNotifier,
    PortfolioAPIClient,
    PortfolioWatcher,
    PortfolioManager,
    get_market_phase,
    can_modify_orders,
    seconds_until_can_modify,
    PREMARKET_START,
    ORDER_FREEZE_START,
    ORDER_FREEZE_END,
    TRADING_START,
    TRADING_END,
)


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def sample_config_section():
    """Sample config section dict."""
    return {
        'username': 'testuser',
        'password': 'testpass',
        'broker': 'bbi',
        'isin': 'IRO1DTRA0001',
        'min_buy_volume': '30000000',
        'sell_discount': '0.99',
        'check_interval': '1.0'
    }


@pytest.fixture
def sample_portfolio_config(sample_config_section):
    """Sample PortfolioConfig instance."""
    return PortfolioConfig.from_config_section('TestSection', sample_config_section)


@pytest.fixture
def sample_best_limits():
    """Sample best limits data."""
    return [
        BestLimit(
            isin='IRO1DTRA0001',
            row=1,
            buy_volume=288479554,
            buy_order_count=1776,
            buy_price=5247.0,
            sell_volume=668,
            sell_order_count=2,
            sell_price=5247.0
        ),
        BestLimit(
            isin='IRO1DTRA0001',
            row=2,
            buy_volume=33952,
            buy_order_count=3,
            buy_price=5246.0,
            sell_volume=0,
            sell_order_count=0,
            sell_price=0
        ),
        BestLimit(
            isin='IRO1DTRA0001',
            row=3,
            buy_volume=13166,
            buy_order_count=3,
            buy_price=5245.0,
            sell_volume=0,
            sell_order_count=0,
            sell_price=0
        ),
    ]


@pytest.fixture
def sample_market_condition(sample_best_limits):
    """Sample MarketCondition instance."""
    return MarketCondition(
        isin='IRO1DTRA0001',
        symbol='داترا',
        last_price=5247.0,
        max_price=5404.0,
        min_price=5090.0,
        best_limits=sample_best_limits,
        total_buy_volume=335597,
        total_sell_volume=668,
        is_queued=False
    )


@pytest.fixture
def sample_portfolio_position():
    """Sample portfolio position."""
    return PortfolioPosition(
        isin='IRO1DTRA0001',
        symbol='داترا',
        quantity=10000,
        average_price=5000.0,
        current_price=5247.0
    )


@pytest.fixture
def mock_api_response_market_data():
    """Mock market data API response."""
    return [{
        "i": {
            "isin": "IRO1DTRA0001",
            "s": "داترا",
            "t": "آترا زیست آرای",
            "maxeq": 400000,
            "mineq": 1
        },
        "t": {
            "isin": "IRO1DTRA0001",
            "maxap": 5404.00,
            "minap": 5090.00,
            "cup": 5247.00,
            "lp": 5247.00
        },
        "bl": [
            {
                "isin": "IRO1DTRA0001",
                "bv": 288479554,
                "boc": 1776,
                "bp": 5247.00,
                "sv": 668,
                "soc": 2,
                "sp": 5247.00,
                "r": 1
            },
            {
                "isin": "IRO1DTRA0001",
                "bv": 33952,
                "boc": 3,
                "bp": 5246.00,
                "sv": 0,
                "soc": 0,
                "sp": 0,
                "r": 2
            }
        ]
    }]


# =============================================================================
# Test OrderState Enum
# =============================================================================

class TestOrderState:
    """Tests for OrderState enum."""
    
    def test_order_state_values(self):
        """Test all order state values."""
        assert OrderState.ADDING == 1
        assert OrderState.ADDED == 2
        assert OrderState.EXECUTED == 3
        assert OrderState.CANCELED == 4
        assert OrderState.ERROR == 5
        assert OrderState.PARTIAL_EXECUTED == 6
        assert OrderState.MODIFIED == 7
        assert OrderState.CANCELING == 8
        assert OrderState.MODIFYING == 9
        assert OrderState.SLE_ERROR == 10
        assert OrderState.DEPRECATED == 11
    
    def test_order_state_from_int(self):
        """Test creating OrderState from integer."""
        assert OrderState(1) == OrderState.ADDING
        assert OrderState(3) == OrderState.EXECUTED
        assert OrderState(6) == OrderState.PARTIAL_EXECUTED


# =============================================================================
# Test Market Timing Functions
# =============================================================================

class TestMarketTiming:
    """Tests for market timing functions."""
    
    def test_market_phase_enum_values(self):
        """Test MarketPhase enum values."""
        assert MarketPhase.CLOSED == 0
        assert MarketPhase.PREMARKET == 1
        assert MarketPhase.ORDER_FREEZE == 2
        assert MarketPhase.TRADING == 3
    
    @patch('portfolio_manager.datetime')
    def test_get_market_phase_premarket(self, mock_datetime):
        """Test premarket phase detection (08:45-08:55)."""
        mock_datetime.now.return_value = datetime(2025, 12, 7, 8, 50, 0)
        mock_datetime.strptime = datetime.strptime
        
        phase = get_market_phase()
        assert phase == MarketPhase.PREMARKET
    
    @patch('portfolio_manager.datetime')
    def test_get_market_phase_order_freeze(self, mock_datetime):
        """Test order freeze phase detection (08:55-09:02)."""
        mock_datetime.now.return_value = datetime(2025, 12, 7, 9, 0, 0)
        mock_datetime.strptime = datetime.strptime
        
        phase = get_market_phase()
        assert phase == MarketPhase.ORDER_FREEZE
    
    @patch('portfolio_manager.datetime')
    def test_get_market_phase_trading(self, mock_datetime):
        """Test trading phase detection (09:02-12:30)."""
        mock_datetime.now.return_value = datetime(2025, 12, 7, 10, 30, 0)
        mock_datetime.strptime = datetime.strptime
        
        phase = get_market_phase()
        assert phase == MarketPhase.TRADING
    
    @patch('portfolio_manager.datetime')
    def test_get_market_phase_closed(self, mock_datetime):
        """Test closed phase detection (outside trading hours)."""
        mock_datetime.now.return_value = datetime(2025, 12, 7, 14, 0, 0)
        mock_datetime.strptime = datetime.strptime
        
        phase = get_market_phase()
        assert phase == MarketPhase.CLOSED
    
    @patch('portfolio_manager.get_market_phase')
    def test_can_modify_orders_premarket(self, mock_phase):
        """Test can modify orders in premarket."""
        mock_phase.return_value = MarketPhase.PREMARKET
        assert can_modify_orders() == True
    
    @patch('portfolio_manager.get_market_phase')
    def test_can_modify_orders_freeze(self, mock_phase):
        """Test cannot modify orders during freeze."""
        mock_phase.return_value = MarketPhase.ORDER_FREEZE
        assert can_modify_orders() == False
    
    @patch('portfolio_manager.get_market_phase')
    def test_can_modify_orders_trading(self, mock_phase):
        """Test can modify orders during trading."""
        mock_phase.return_value = MarketPhase.TRADING
        assert can_modify_orders() == True


# =============================================================================
# Test PortfolioConfig
# =============================================================================

class TestPortfolioConfig:
    """Tests for PortfolioConfig dataclass."""
    
    def test_from_config_section_full(self, sample_config_section):
        """Test creating config from full config section."""
        config = PortfolioConfig.from_config_section('Test', sample_config_section)
        
        assert config.section_name == 'Test'
        assert config.username == 'testuser'
        assert config.password == 'testpass'
        assert config.broker == 'bbi'
        assert config.isin == 'IRO1DTRA0001'
        assert config.min_buy_volume == 30000000
        assert config.sell_discount == 0.99
        assert config.check_interval == 1.0
    
    def test_from_config_section_defaults(self):
        """Test default values when not specified."""
        minimal_section = {
            'username': 'user',
            'password': 'pass',
            'isin': 'IRO1TEST0001'
        }
        config = PortfolioConfig.from_config_section('Minimal', minimal_section)
        
        assert config.broker == 'bbi'  # Default broker
        assert config.min_buy_volume == 30_000_000  # Default
        assert config.sell_discount == 0.99  # Default
        assert config.check_interval == 1.0  # Default
    
    def test_from_config_section_custom_values(self):
        """Test custom values."""
        section = {
            'username': 'user',
            'password': 'pass',
            'broker': 'gs',
            'isin': 'IRO1TEST0001',
            'min_buy_volume': '50000000',
            'sell_discount': '0.98',
            'check_interval': '0.5'
        }
        config = PortfolioConfig.from_config_section('Custom', section)
        
        assert config.broker == 'gs'
        assert config.min_buy_volume == 50000000
        assert config.sell_discount == 0.98
        assert config.check_interval == 0.5


# =============================================================================
# Test BestLimit Dataclass
# =============================================================================

class TestBestLimit:
    """Tests for BestLimit dataclass."""
    
    def test_best_limit_creation(self):
        """Test creating a BestLimit."""
        bl = BestLimit(
            isin='IRO1TEST0001',
            row=1,
            buy_volume=1000000,
            buy_order_count=100,
            buy_price=5000.0,
            sell_volume=500,
            sell_order_count=5,
            sell_price=5010.0
        )
        
        assert bl.isin == 'IRO1TEST0001'
        assert bl.row == 1
        assert bl.buy_volume == 1000000
        assert bl.buy_order_count == 100
        assert bl.buy_price == 5000.0
        assert bl.sell_volume == 500
        assert bl.sell_order_count == 5
        assert bl.sell_price == 5010.0


# =============================================================================
# Test MarketCondition
# =============================================================================

class TestMarketCondition:
    """Tests for MarketCondition dataclass."""
    
    def test_best_buy_price(self, sample_market_condition):
        """Test best_buy_price property."""
        assert sample_market_condition.best_buy_price == 5247.0
    
    def test_best_sell_price(self, sample_market_condition):
        """Test best_sell_price property."""
        assert sample_market_condition.best_sell_price == 5247.0
    
    def test_first_row_buy_volume(self, sample_market_condition):
        """Test first_row_buy_volume property."""
        assert sample_market_condition.first_row_buy_volume == 288479554
    
    def test_empty_best_limits(self):
        """Test properties with empty best limits."""
        condition = MarketCondition(
            isin='IRO1TEST0001',
            symbol='TEST',
            last_price=5000.0,
            max_price=5500.0,
            min_price=4500.0,
            best_limits=[],
            total_buy_volume=0,
            total_sell_volume=0,
            is_queued=True
        )
        
        assert condition.best_buy_price == 0.0
        assert condition.best_sell_price == 0.0
        assert condition.first_row_buy_volume == 0
    
    def test_is_queued_detection(self):
        """Test queue detection logic."""
        # Queued market - no sell orders
        queued = MarketCondition(
            isin='IRO1TEST0001',
            symbol='TEST',
            last_price=5000.0,
            max_price=5500.0,
            min_price=4500.0,
            best_limits=[],
            total_buy_volume=1000000,
            total_sell_volume=0,
            is_queued=True
        )
        assert queued.is_queued == True
        
        # Normal market - has sell orders
        normal = MarketCondition(
            isin='IRO1TEST0001',
            symbol='TEST',
            last_price=5000.0,
            max_price=5500.0,
            min_price=4500.0,
            best_limits=[BestLimit(
                isin='IRO1TEST0001', row=1,
                buy_volume=1000, buy_order_count=10, buy_price=5000.0,
                sell_volume=500, sell_order_count=5, sell_price=5010.0
            )],
            total_buy_volume=1000,
            total_sell_volume=500,
            is_queued=False
        )
        assert normal.is_queued == False
    
    def test_is_sell_queue_detection(self):
        """Test sell queue detection - when sell at min price with heavy selling."""
        # Sell queue (panic) - selling at min price with heavy sell volume
        sell_queue = MarketCondition(
            isin='IRO1TEST0001',
            symbol='TEST',
            last_price=5000.0,
            max_price=5500.0,
            min_price=4500.0,
            best_limits=[BestLimit(
                isin='IRO1TEST0001', row=1,
                buy_volume=100_000, buy_order_count=10, buy_price=4500.0,  # Weak buying
                sell_volume=50_000_000, sell_order_count=5000, sell_price=4500.0  # Sell at min!
            )],
            total_buy_volume=500_000,   # Very weak buying (< 1M)
            total_sell_volume=200_000_000,  # 200M
            is_queued=False
        )
        assert sell_queue.is_sell_queue == True
        
        # Normal market - sell price not at min, balanced volumes
        normal = MarketCondition(
            isin='IRO1TEST0001',
            symbol='TEST',
            last_price=5000.0,
            max_price=5500.0,
            min_price=4500.0,
            best_limits=[BestLimit(
                isin='IRO1TEST0001', row=1,
                buy_volume=1_000_000, buy_order_count=100, buy_price=4950.0,
                sell_volume=500_000, sell_order_count=50, sell_price=5010.0  # Sell at 5010, not min
            )],
            total_buy_volume=100_000_000,  # 100M healthy buying
            total_sell_volume=50_000_000,   # 50M
            is_queued=False
        )
        assert normal.is_sell_queue == False
        
        # Empty best_limits - should be False
        empty = MarketCondition(
            isin='IRO1TEST0001',
            symbol='TEST',
            last_price=5000.0,
            max_price=5500.0,
            min_price=4500.0,
            best_limits=[],
            total_buy_volume=10_000_000,
            total_sell_volume=100_000_000,
            is_queued=False
        )
        assert empty.is_sell_queue == False  # No best_limits = can't determine


# =============================================================================
# Test SellOrder
# =============================================================================

class TestSellOrder:
    """Tests for SellOrder dataclass."""
    
    def test_sell_order_creation(self):
        """Test creating a sell order."""
        order = SellOrder(
            isin='IRO1TEST0001',
            price=5000.0,
            volume=1000
        )
        
        assert order.isin == 'IRO1TEST0001'
        assert order.price == 5000.0
        assert order.volume == 1000
        assert order.remaining_volume == 1000
        assert order.serial_number is None
        assert order.state is None
    
    def test_sell_order_remaining_volume(self):
        """Test remaining volume calculation."""
        order = SellOrder(
            isin='IRO1TEST0001',
            price=5000.0,
            volume=1000,
            executed_volume=300
        )
        
        assert order.remaining_volume == 700
    
    def test_sell_order_with_state(self):
        """Test sell order with state."""
        order = SellOrder(
            isin='IRO1TEST0001',
            price=5000.0,
            volume=1000,
            serial_number=12345,
            state=OrderState.ADDED
        )
        
        assert order.serial_number == 12345
        assert order.state == OrderState.ADDED


# =============================================================================
# Test PortfolioPosition
# =============================================================================

class TestPortfolioPosition:
    """Tests for PortfolioPosition dataclass."""
    
    def test_market_value(self, sample_portfolio_position):
        """Test market value calculation."""
        assert sample_portfolio_position.market_value == 10000 * 5247.0
    
    def test_market_value_zero_price(self):
        """Test market value with zero price."""
        position = PortfolioPosition(
            isin='IRO1TEST0001',
            symbol='TEST',
            quantity=1000,
            average_price=5000.0,
            current_price=0.0
        )
        assert position.market_value == 0.0


# =============================================================================
# Test TelegramNotifier
# =============================================================================

class TestTelegramNotifier:
    """Tests for TelegramNotifier."""
    
    def test_send_without_credentials(self):
        """Test sending without credentials configured."""
        with patch.dict(os.environ, {}, clear=True):
            notifier = TelegramNotifier()
            result = notifier.send("Test message")
            assert result == False
    
    @patch('portfolio_manager.requests.post')
    def test_send_success(self, mock_post):
        """Test successful send."""
        mock_post.return_value.status_code = 200
        
        with patch.dict(os.environ, {
            'TELEGRAM_BOT_TOKEN': 'test_token',
            'TELEGRAM_USER_ID': '123456'
        }):
            notifier = TelegramNotifier()
            result = notifier.send("Test message")
            assert result == True
            mock_post.assert_called_once()
    
    @patch('portfolio_manager.requests.post')
    def test_send_failure(self, mock_post):
        """Test failed send."""
        mock_post.return_value.status_code = 400
        
        with patch.dict(os.environ, {
            'TELEGRAM_BOT_TOKEN': 'test_token',
            'TELEGRAM_USER_ID': '123456'
        }):
            notifier = TelegramNotifier()
            result = notifier.send("Test message")
            assert result == False


# =============================================================================
# Test PortfolioAPIClient
# =============================================================================

class TestPortfolioAPIClient:
    """Tests for PortfolioAPIClient."""
    
    @pytest.fixture
    def mock_api_client(self):
        """Create a mocked API client."""
        with patch.object(PortfolioAPIClient, '__init__', lambda self, **kwargs: None):
            client = PortfolioAPIClient()
            client.broker_code = 'bbi'
            client.username = 'testuser'
            client.password = 'testpass'
            client.endpoints = {
                'portfolio': 'https://backofficeexternal-bbi.ephoenix.ir/api/portfolio/getrealsecuritypositionbydate',
                'market_data': 'https://mdapi1.ephoenix.ir/api/v2/instruments/full',
                'cancel_order': 'https://api-bbi.ephoenix.ir/api/v2/orders/CancelOrder',
                'order': 'https://api-bbi.ephoenix.ir/api/v2/orders/NewOrder',
                'open_orders': 'https://api-bbi.ephoenix.ir/api/v2/orders/GetOpenOrders'
            }
            client.token = 'test_token'
            client.cache = None
            return client
    
    @patch('portfolio_manager.requests.post')
    def test_get_best_limits(self, mock_post, mock_api_client, mock_api_response_market_data):
        """Test getting best limits."""
        mock_api_client.authenticate = Mock(return_value='test_token')
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = mock_api_response_market_data
        mock_post.return_value.raise_for_status = Mock()
        
        result = mock_api_client.get_best_limits('IRO1DTRA0001')
        
        assert result.isin == 'IRO1DTRA0001'
        assert result.symbol == 'داترا'
        assert result.last_price == 5247.0
        assert result.max_price == 5404.0
        assert result.min_price == 5090.0
        assert len(result.best_limits) == 2
        assert result.best_limits[0].buy_volume == 288479554
    
    @patch('portfolio_manager.requests.delete')
    def test_cancel_order_success(self, mock_delete, mock_api_client):
        """Test successful order cancellation."""
        mock_api_client.authenticate = Mock(return_value='test_token')
        mock_delete.return_value.status_code = 200
        mock_delete.return_value.raise_for_status = Mock()
        
        result = mock_api_client.cancel_order(12345)
        
        assert result == True
        mock_delete.assert_called_once()
    
    @patch('portfolio_manager.requests.delete')
    def test_cancel_order_failure(self, mock_delete, mock_api_client):
        """Test failed order cancellation."""
        mock_api_client.authenticate = Mock(return_value='test_token')
        mock_delete.side_effect = Exception("Network error")
        
        result = mock_api_client.cancel_order(12345)
        
        assert result == False
    
    @patch('portfolio_manager.requests.post')
    def test_place_sell_order_success(self, mock_post, mock_api_client):
        """Test successful sell order placement."""
        mock_api_client.authenticate = Mock(return_value='test_token')
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {'serialNumber': 99999}
        mock_post.return_value.raise_for_status = Mock()
        
        result = mock_api_client.place_sell_order('IRO1TEST0001', 5000.0, 1000)
        
        assert result is not None
        assert result['serialNumber'] == 99999


# =============================================================================
# Test PortfolioWatcher
# =============================================================================

class TestPortfolioWatcher:
    """Tests for PortfolioWatcher."""
    
    @pytest.fixture
    def mock_watcher(self, sample_portfolio_config):
        """Create a mocked portfolio watcher."""
        with patch('portfolio_manager.PortfolioAPIClient'):
            with patch('portfolio_manager.TradingCache'):
                with patch('portfolio_manager.TelegramNotifier'):
                    watcher = PortfolioWatcher(
                        config=sample_portfolio_config,
                        cache=Mock(),
                        notifier=Mock()
                    )
                    watcher.api_client = Mock()
                    return watcher
    
    def test_determine_sell_action_queue_demand_low(self, mock_watcher, sample_market_condition):
        """Test sell action when queue demand is low."""
        # Stock is queued but buy volume is below threshold
        sample_market_condition.is_queued = True
        mock_watcher.config.min_buy_volume = 999_999_999_999  # Very high threshold
        
        price, reason = mock_watcher._determine_sell_action(sample_market_condition, MarketPhase.TRADING)
        
        assert price is not None
        assert reason == "queue_demand_low"
        assert price == sample_market_condition.best_buy_price
    
    def test_determine_sell_action_queue_demand_high(self, mock_watcher, sample_market_condition):
        """Test no sell action when queue demand is high (hold)."""
        # Stock is queued and buy volume is above threshold
        sample_market_condition.is_queued = True
        mock_watcher.config.min_buy_volume = 1000000  # Low threshold, demand is high
        
        price, reason = mock_watcher._determine_sell_action(sample_market_condition, MarketPhase.TRADING)
        
        assert price is None
        assert reason is None
    
    def test_determine_sell_action_normal_market(self, mock_watcher, sample_market_condition):
        """Test sell action in normal market (not queued)."""
        sample_market_condition.is_queued = False
        
        price, reason = mock_watcher._determine_sell_action(sample_market_condition, MarketPhase.TRADING)
        
        assert price is not None
        assert reason == "normal_market_sell"
        assert price == pytest.approx(sample_market_condition.last_price * mock_watcher.config.sell_discount)
    
    def test_determine_sell_action_premarket_normal(self, mock_watcher, sample_market_condition):
        """Test sell action in premarket when stock is normal."""
        sample_market_condition.is_queued = False
        
        price, reason = mock_watcher._determine_sell_action(sample_market_condition, MarketPhase.PREMARKET)
        
        assert price is not None
        assert reason == "premarket_normal_urgent"
    
    def test_determine_sell_action_sell_queue_panic(self, mock_watcher, sample_market_condition):
        """Test sell action in sell queue (panic mode) - must sell at MIN price."""
        # Set up sell queue conditions:
        # 1. Sell price at min price
        # 2. Heavy sell volume OR weak buy volume
        sample_market_condition.min_price = 4500.0
        sample_market_condition.best_limits = [BestLimit(
            isin='IRO1TEST0001', row=1,
            buy_volume=100_000, buy_order_count=10, buy_price=4500.0,  # Weak buying
            sell_volume=50_000_000, sell_order_count=5000, sell_price=4500.0  # Sell at min!
        )]
        sample_market_condition.total_sell_volume = 500_000_000  # Heavy selling
        sample_market_condition.total_buy_volume = 500_000       # Very weak buying (<1M)
        sample_market_condition.is_queued = False
        
        price, reason = mock_watcher._determine_sell_action(sample_market_condition, MarketPhase.TRADING)
        
        assert price == sample_market_condition.min_price
        assert reason == "sell_queue_panic"
    
    def test_determine_sell_action_sell_queue_premarket(self, mock_watcher, sample_market_condition):
        """Test sell action in sell queue during premarket (urgent)."""
        # Set up sell queue conditions for premarket
        sample_market_condition.min_price = 4500.0
        sample_market_condition.best_limits = [BestLimit(
            isin='IRO1TEST0001', row=1,
            buy_volume=50_000, buy_order_count=5, buy_price=4500.0,
            sell_volume=100_000_000, sell_order_count=10000, sell_price=4500.0  # Panic at min!
        )]
        sample_market_condition.total_sell_volume = 500_000_000
        sample_market_condition.total_buy_volume = 100_000  # Very weak (<1M)
        sample_market_condition.is_queued = False
        
        price, reason = mock_watcher._determine_sell_action(sample_market_condition, MarketPhase.PREMARKET)
        
        assert price == sample_market_condition.min_price
        assert reason == "sell_queue_panic_premarket"
    
    def test_determine_sell_action_queue_demand_low_premarket(self, mock_watcher, sample_market_condition):
        """Test sell action when queue demand low during premarket."""
        sample_market_condition.is_queued = True
        mock_watcher.config.min_buy_volume = 999_999_999_999  # Very high threshold
        
        price, reason = mock_watcher._determine_sell_action(sample_market_condition, MarketPhase.PREMARKET)
        
        assert price is not None
        assert reason == "queue_demand_low_premarket"
        assert price == sample_market_condition.best_buy_price
    
    def test_determine_sell_action_price_floor_at_min_price(self, mock_watcher, sample_market_condition):
        """Test that sell price never goes below daily minimum price.
        
        Edge case: When last_price * sell_discount < min_price, 
        the bot should use min_price instead.
        """
        # Set up scenario where discounted price would be below min
        sample_market_condition.is_queued = False
        sample_market_condition.last_price = 3200.0
        sample_market_condition.min_price = 3180.0  # -0.63% from last
        mock_watcher.config.sell_discount = 0.99  # -1% discount
        
        # Calculated price = 3200 * 0.99 = 3168 < min_price (3180)
        price, reason = mock_watcher._determine_sell_action(sample_market_condition, MarketPhase.TRADING)
        
        # Should use min_price as floor
        assert price == sample_market_condition.min_price
        assert price == 3180.0
        assert reason == "normal_market_sell"
    
    def test_determine_sell_action_price_floor_premarket(self, mock_watcher, sample_market_condition):
        """Test min price floor in premarket phase."""
        sample_market_condition.is_queued = False
        sample_market_condition.last_price = 5000.0
        sample_market_condition.min_price = 4980.0  # Very tight range
        mock_watcher.config.sell_discount = 0.99  # Would give 4950
        
        # Calculated price = 5000 * 0.99 = 4950 < min_price (4980)
        price, reason = mock_watcher._determine_sell_action(sample_market_condition, MarketPhase.PREMARKET)
        
        assert price == sample_market_condition.min_price
        assert price == 4980.0
        assert reason == "premarket_normal_urgent"
    
    def test_determine_sell_action_price_above_min_ok(self, mock_watcher, sample_market_condition):
        """Test that normal discounted price is used when above min."""
        sample_market_condition.is_queued = False
        sample_market_condition.last_price = 5000.0
        sample_market_condition.min_price = 4500.0  # Wide range (-10%)
        mock_watcher.config.sell_discount = 0.99  # -1% discount
        
        # Calculated price = 5000 * 0.99 = 4950 > min_price (4500)
        price, reason = mock_watcher._determine_sell_action(sample_market_condition, MarketPhase.TRADING)
        
        # Should use calculated discounted price
        assert price == pytest.approx(5000.0 * 0.99)
        assert price == 4950.0
        assert reason == "normal_market_sell"
    
    def test_get_pending_volume(self, mock_watcher):
        """Test pending volume calculation."""
        mock_watcher._pending_orders = [
            SellOrder('IRO1TEST0001', 5000.0, 1000, remaining_volume=500),
            SellOrder('IRO1TEST0001', 5000.0, 2000, remaining_volume=1500),
        ]
        # Need to manually set remaining_volume as __post_init__ calculates it
        mock_watcher._pending_orders[0].remaining_volume = 500
        mock_watcher._pending_orders[1].remaining_volume = 1500
        
        total = mock_watcher._get_pending_volume()
        
        assert total == 2000
    
    def test_start_stop(self, mock_watcher):
        """Test starting and stopping the watcher."""
        mock_watcher.start()
        assert mock_watcher.is_running == True
        
        mock_watcher.stop()
        assert mock_watcher.is_running == False


# =============================================================================
# Test Volume Splitting
# =============================================================================

class TestVolumeSplitting:
    """Tests for volume splitting logic in sell orders."""
    
    @pytest.fixture
    def mock_watcher_for_splitting(self, sample_portfolio_config):
        """Create watcher for volume splitting tests."""
        with patch('portfolio_manager.PortfolioAPIClient'):
            with patch('portfolio_manager.TradingCache'):
                with patch('portfolio_manager.TelegramNotifier'):
                    watcher = PortfolioWatcher(
                        config=sample_portfolio_config,
                        cache=Mock(),
                        notifier=Mock()
                    )
                    watcher.api_client = Mock()
                    watcher.api_client.get_instrument_info = Mock(return_value={
                        'max_volume': 400000
                    })
                    watcher.api_client.place_sell_order = Mock(return_value={
                        'serialNumber': 12345
                    })
                    return watcher
    
    def test_split_single_order(self, mock_watcher_for_splitting, sample_market_condition):
        """Test no split needed for small volume."""
        mock_watcher_for_splitting._execute_sell(
            sample_market_condition, 5000.0, 100000, "test"
        )
        
        # Should place only one order
        assert mock_watcher_for_splitting.api_client.place_sell_order.call_count == 1
    
    def test_split_multiple_orders(self, mock_watcher_for_splitting, sample_market_condition):
        """Test splitting into multiple orders."""
        mock_watcher_for_splitting._execute_sell(
            sample_market_condition, 5000.0, 1000000, "test"
        )
        
        # 1,000,000 / 400,000 = 3 orders needed
        assert mock_watcher_for_splitting.api_client.place_sell_order.call_count == 3
    
    def test_split_exact_multiple(self, mock_watcher_for_splitting, sample_market_condition):
        """Test splitting exact multiple of max volume."""
        mock_watcher_for_splitting._execute_sell(
            sample_market_condition, 5000.0, 800000, "test"
        )
        
        # 800,000 / 400,000 = 2 orders exactly
        assert mock_watcher_for_splitting.api_client.place_sell_order.call_count == 2


# =============================================================================
# Test PortfolioManager
# =============================================================================

class TestPortfolioManager:
    """Tests for PortfolioManager."""
    
    @pytest.fixture
    def temp_config_file(self, tmp_path):
        """Create a temporary config file."""
        config_content = """
[Portfolio_Test1]
username = user1
password = pass1
broker = bbi
isin = IRO1TEST0001
min_buy_volume = 30000000

[Portfolio_Test2]
username = user2
password = pass2
broker = gs
isin = IRO1TEST0002
min_buy_volume = 50000000
"""
        config_file = tmp_path / "test_portfolio_config.ini"
        config_file.write_text(config_content)
        return str(config_file)
    
    def test_load_configs(self, temp_config_file):
        """Test loading configurations."""
        manager = PortfolioManager(temp_config_file)
        configs = manager.load_configs()
        
        assert len(configs) == 2
        assert configs[0].section_name == 'Portfolio_Test1'
        assert configs[0].username == 'user1'
        assert configs[1].section_name == 'Portfolio_Test2'
        assert configs[1].broker == 'gs'
    
    def test_get_status_empty(self, temp_config_file):
        """Test getting status with no watchers."""
        manager = PortfolioManager(temp_config_file)
        status = manager.get_status()
        
        assert status == {}
    
    @patch('portfolio_manager.PortfolioWatcher')
    def test_start_all(self, mock_watcher_class, temp_config_file):
        """Test starting all watchers."""
        mock_watcher = Mock()
        mock_watcher_class.return_value = mock_watcher
        
        manager = PortfolioManager(temp_config_file)
        manager.start_all()
        
        assert len(manager.watchers) == 2
        assert mock_watcher.start.call_count == 2
    
    @patch('portfolio_manager.PortfolioWatcher')
    def test_stop_all(self, mock_watcher_class, temp_config_file):
        """Test stopping all watchers."""
        mock_watcher = Mock()
        mock_watcher_class.return_value = mock_watcher
        
        manager = PortfolioManager(temp_config_file)
        manager.start_all()
        manager.stop_all()
        
        assert len(manager.watchers) == 0
        assert mock_watcher.stop.call_count == 2


# =============================================================================
# Test Pre-Market Normal Detection
# =============================================================================

class TestPreMarketNormal:
    """Tests for pre-market normal stock detection."""
    
    @pytest.fixture
    def watcher_with_mocked_time(self, sample_portfolio_config):
        """Create watcher with mocked datetime."""
        with patch('portfolio_manager.PortfolioAPIClient'):
            with patch('portfolio_manager.TradingCache'):
                with patch('portfolio_manager.TelegramNotifier'):
                    watcher = PortfolioWatcher(
                        config=sample_portfolio_config,
                        cache=Mock(),
                        notifier=Mock()
                    )
                    watcher.api_client = Mock()
                    return watcher
    
    @patch('portfolio_manager.datetime')
    def test_pre_market_normal_sell(self, mock_datetime, watcher_with_mocked_time):
        """Test sell decision during pre-market when stock is normal."""
        # Set time to before market open (8:44)
        mock_now = datetime(2025, 12, 7, 8, 30, 0)
        mock_datetime.now.return_value = mock_now
        
        market_condition = MarketCondition(
            isin='IRO1DTRA0001',
            symbol='داترا',
            last_price=5247.0,
            max_price=5404.0,
            min_price=5090.0,
            best_limits=[],
            total_buy_volume=100000,
            total_sell_volume=100,
            is_queued=False  # Stock is normal pre-market
        )
        
        # Test with PREMARKET phase directly
        price, reason = watcher_with_mocked_time._determine_sell_action(market_condition, MarketPhase.PREMARKET)
        
        assert reason == "premarket_normal_urgent"
        assert price == pytest.approx(5247.0 * 0.99)


# =============================================================================
# Test Order State Tracking
# =============================================================================

class TestOrderStateTracking:
    """Tests for order state tracking."""
    
    @pytest.fixture
    def watcher_with_pending_orders(self, sample_portfolio_config):
        """Create watcher with pending orders."""
        with patch('portfolio_manager.PortfolioAPIClient'):
            with patch('portfolio_manager.TradingCache'):
                with patch('portfolio_manager.TelegramNotifier'):
                    watcher = PortfolioWatcher(
                        config=sample_portfolio_config,
                        cache=Mock(),
                        notifier=Mock()
                    )
                    watcher.api_client = Mock()
                    watcher._pending_orders = [
                        SellOrder('IRO1TEST0001', 5000.0, 1000, serial_number=111),
                        SellOrder('IRO1TEST0001', 5000.0, 2000, serial_number=222),
                    ]
                    return watcher
    
    def test_update_executed_orders(self, watcher_with_pending_orders):
        """Test updating executed orders."""
        # First order is executed (not in open orders), second is still pending
        watcher_with_pending_orders.api_client.get_order_status = Mock(
            side_effect=[None, {'orderState': 2, 'executedVolume': 0}]
        )
        
        watcher_with_pending_orders._update_pending_orders()
        
        assert watcher_with_pending_orders._total_sold == 1000
        assert len(watcher_with_pending_orders._pending_orders) == 1
    
    def test_update_partial_executed(self, watcher_with_pending_orders):
        """Test updating partially executed orders."""
        watcher_with_pending_orders.api_client.get_order_status = Mock(
            return_value={'orderState': 6, 'executedVolume': 500}  # Partial
        )
        
        # Clear pending orders and add just one
        watcher_with_pending_orders._pending_orders = [
            SellOrder('IRO1TEST0001', 5000.0, 1000, serial_number=111)
        ]
        
        watcher_with_pending_orders._update_pending_orders()
        
        assert watcher_with_pending_orders._total_sold == 500
        assert len(watcher_with_pending_orders._pending_orders) == 1


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
