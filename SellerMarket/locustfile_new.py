"""
Enhanced Locust load testing for stock trading with dynamic order calculation.

Features:
- Automatic price fetching from market data
- Dynamic volume calculation based on buying power
- Simplified configuration
- Comprehensive logging
- Order result tracking
"""

from locust import HttpUser, task, events
import json
import requests
import configparser
import logging
from collections import namedtuple
from typing import Dict, Any

from broker_enum import BrokerCode
from api_client import EphoenixAPIClient
from order_tracker import OrderResultTracker, OrderResult
from cache_manager import TradingCache

# Configure logging - truncate log file on each run
_log_file_path = 'trading_bot.log'

# Truncate the log file at module load
with open(_log_file_path, 'w', encoding='utf-8') as f:
    f.write('')  # Clear the file

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(_log_file_path, mode='a', encoding='utf-8'),  # Append mode after truncation
        logging.StreamHandler()
    ],
    force=True  # Override any existing configuration
)

logger = logging.getLogger(__name__)

# Store the file handler globally so we can ensure it's always used
_file_handler = None
for handler in logging.getLogger().handlers:
    if isinstance(handler, logging.FileHandler):
        _file_handler = handler
        break

# Ensure our logger always uses the file handler
if _file_handler and _file_handler not in logger.handlers:
    logger.addHandler(_file_handler)
    logger.setLevel(logging.INFO)

# Also configure Locust's loggers to use our handlers
for logger_name in ['locust.main', 'locust.runners', 'locust.user.users', 'locustfile_new']:
    locust_logger = logging.getLogger(logger_name)
    locust_logger.setLevel(logging.INFO)
    # Ensure propagation so it uses root logger's handlers
    locust_logger.propagate = True

# Global order tracker
order_tracker = OrderResultTracker()

# Global cache manager
cache_manager = TradingCache()


def decode_captcha(im: str) -> str:
    """
    Decode captcha image using OCR service.
    
    Args:
        im: Base64 encoded image
        
    Returns:
        Decoded captcha text
    """
    url = 'http://localhost:8080/ocr/by-base64'
    headers = {
        'accept': 'text/plain',
        'Content-Type': 'application/json'
    }
    data = {"base64": im}
    
    try:
        response = requests.post(url, headers=headers, json=data)
        result = response.text
        logger.debug(f"Captcha decoded: {result}")
        return "".join(result)
    except requests.RequestException as e:
        logger.error(f"Captcha decoding failed: {e}")
        return ""


def prepare_order_data(config_section: dict) -> Dict[str, Any]:
    """
    Prepare order data with dynamic price and volume calculation.
    
    Args:
        config_section: Configuration section from INI file
        
    Returns:
        Dictionary with order URL, token, and data
    """
    username = config_section['username']
    password = config_section['password']
    broker_code = config_section['broker']
    isin = config_section['isin']
    side = int(config_section['side'])
    
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
        captcha_decoder=decode_captcha,
        endpoints=endpoints,
        cache=cache_manager
    )
    
    # Step 1: Authenticate
    logger.info("Step 1: Authenticating...")
    token = api_client.authenticate()
    logger.info("✓ Authentication successful")
    
    # Step 2: Get buying power
    logger.info("Step 2: Fetching buying power...")
    buying_power = api_client.get_buying_power()
    logger.info(f"✓ Buying power: {buying_power:,.0f} Rials")
    
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
    
    # Step 4: Calculate volume
    logger.info("Step 4: Calculating order volume...")
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
        logger.warning(f"⚠ Volume constrained from {calculated_volume:,} to {volume:,} (max allowed)")
    else:
        logger.info(f"✓ Calculated volume: {volume:,} shares")
    
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
    
    logger.info(f"✓ Order prepared:")
    logger.info(f"  ISIN: {isin}")
    logger.info(f"  Side: {'Buy' if side == 1 else 'Sell'}")
    logger.info(f"  Price: {price:,} Rials")
    logger.info(f"  Volume: {volume:,} shares")
    logger.info(f"  Total: {price * volume:,.0f} Rials")
    logger.info(f"{'='*80}\n")
    
    OrderData = namedtuple('OrderData', 'order_url token data username broker_code isin api_client')
    return OrderData(
        order_url=endpoints['order'],
        token=token,
        data=order_json,
        username=username,
        broker_code=broker_code,
        isin=isin,
        api_client=api_client
    )


class TradingUser(HttpUser):
    """Base Locust user for trading operations."""
    
    abstract = True
    
    def populate(self, order_data: namedtuple):
        """
        Populate user with order data.
        
        Args:
            order_data: Named tuple with order information
        """
        self.order_url = order_data.order_url
        self.token = order_data.token
        self.order_json = order_data.data
        self.username = order_data.username
        self.broker_code = order_data.broker_code
        self.isin = order_data.isin
        self.api_client = order_data.api_client
    
    @task
    def place_order(self):
        """Execute order placement task."""
        # Get logger with file handler for this task
        task_logger = logging.getLogger(__name__)
        
        try:
            task_logger.info(f"Placing order for {self.username}@{self.broker_code}")
            
            response = self.client.request(
                method="POST",
                url=self.order_url,
                name=f"{self.username}@{self.broker_code}",
                data=self.order_json,
                headers={
                    "authorization": f"Bearer {self.token}",
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            )
            
            if response.status_code == 200:
                task_logger.info(f"✓ Order placed successfully for {self.username}@{self.broker_code}")
                task_logger.debug(f"Response: {response.text}")
            else:
                task_logger.error(f"✗ Order failed for {self.username}@{self.broker_code}: "
                           f"Status {response.status_code}")
                task_logger.error(f"Response: {response.text}")
                
        except Exception as e:
            task_logger.error(f"✗ Exception during order placement for {self.username}@{self.broker_code}: {e}")


@events.init.add_listener
def on_locust_init(environment, **kwargs):
    """
    Event handler called when Locust initializes.
    Ensures Locust's loggers write to our log file.
    """
    # Get the file handler from root logger
    root_logger = logging.getLogger()
    file_handler = None
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            file_handler = handler
            break
    
    if file_handler:
        # Add file handler to all Locust loggers
        for logger_name in ['locust.main', 'locust.runners', 'locust.user.users', 
                           'locust.stats', 'locust.stats_logger', 'locust']:
            locust_logger = logging.getLogger(logger_name)
            # Remove existing handlers to avoid duplicates
            locust_logger.handlers = []
            # Add our file handler
            locust_logger.addHandler(file_handler)
            # Also add stream handler for console output
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(logging.Formatter('[%(asctime)s] %(name)s/%(levelname)s/%(message)s'))
            locust_logger.addHandler(stream_handler)
            locust_logger.setLevel(logging.INFO)
            locust_logger.propagate = False  # Don't propagate to avoid duplicates


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """
    Event handler called when load test starts.
    Additional check to ensure Locust loggers are configured.
    """
    # Get the file handler from root logger
    root_logger = logging.getLogger()
    file_handler = None
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            file_handler = handler
            break
    
    if file_handler:
        # Ensure all Locust loggers have our file handler
        for logger_name in ['locust.main', 'locust.runners', 'locust.user.users']:
            locust_logger = logging.getLogger(logger_name)
            if file_handler not in locust_logger.handlers:
                locust_logger.addHandler(file_handler)
                locust_logger.setLevel(logging.INFO)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """
    Event handler called when load test stops.
    Fetches and saves order results for all users.
    """
    logger.info("\n" + "="*80)
    logger.info("TEST STOPPED - Fetching order results...")
    logger.info("="*80 + "\n")
    
    # Get all user classes
    for section_name in config.sections():
        section = dict(config[section_name])
        username = section['username']
        broker_code = section['broker']
        
        try:
            # Get broker endpoints
            broker_enum = BrokerCode(broker_code)
            endpoints = broker_enum.get_endpoints()
            
            # Create API client
            api_client = EphoenixAPIClient(
                broker_code=broker_code,
                username=username,
                password=section['password'],
                captcha_decoder=decode_captcha,
                endpoints=endpoints
            )
            
            logger.info(f"Fetching orders for {username}@{broker_code}...")
            
            # Get open orders
            orders_data = api_client.get_open_orders()
            orders = [OrderResult(order_data) for order_data in orders_data]
            
            # Save results
            order_tracker.save_order_results(username, broker_code, orders)
            
            # Print summary
            summary = order_tracker.get_summary_report(username, broker_code)
            logger.info(summary)
            
        except Exception as e:
            logger.error(f"Failed to fetch orders for {username}@{broker_code}: {e}")
    
    logger.info("\n" + "="*80)
    logger.info("Order results saved. Check 'order_results' directory for details.")
    logger.info("="*80 + "\n")


# Load configuration
config = configparser.ConfigParser()
config.read('config.ini')

if not config.sections():
    logger.error("No configuration found in config.ini!")
    logger.error("Please copy config.simple.example.ini to config.ini and configure your accounts.")
    exit(1)

logger.info(f"Loaded configuration with {len(config.sections())} account(s)")

# Dynamically create user classes for each config section
def _create_user_classes():
    """Create user classes in a function scope to avoid variable leakage to globals."""
    import sys
    current_module = sys.modules[__name__]
    user_classes = []
    
    for idx, section_name in enumerate(config.sections(), start=1):
        try:
            section = dict(config[section_name])
            
            # Prepare order data
            order_data = prepare_order_data(section)
            
            # Create unique class name with index to absolutely avoid conflicts
            base_class_name = section_name.replace('-', '_').replace('.', '_').replace(' ', '_')
            unique_class_name = f"{base_class_name}_User{idx}"
            
            # Create dynamic user class with unique name and set class attributes
            user_class = type(unique_class_name, (TradingUser,), {
                'order_url': order_data.order_url,
                'token': order_data.token,
                'order_json': order_data.data,
                'username': order_data.username,
                'broker_code': order_data.broker_code,
                'isin': order_data.isin,
                'api_client': order_data.api_client,
                '__module__': __name__,  # Explicitly set module
                '__qualname__': unique_class_name,  # Explicitly set qualified name
            })
            
            # Register as module attribute using setattr instead of globals()
            setattr(current_module, unique_class_name, user_class)
            user_classes.append(unique_class_name)
            
            logger.info(f"✓ Configured trading user: {unique_class_name}")
            
        except Exception as e:
            logger.error(f"✗ Failed to configure {section_name}: {e}")
            logger.exception(e)
    
    return user_classes

# Create all user classes
_configured_users = _create_user_classes()

logger.info("\n" + "="*80)
logger.info("All users configured. Ready to start load test.")
logger.info("="*80 + "\n")
