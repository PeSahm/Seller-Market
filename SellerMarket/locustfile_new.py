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
from typing import Dict, Any, Optional
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from broker_enum import BrokerCode, get_endpoints_for
from locust_scaling import per_section_user_count
from api_client import EphoenixAPIClient
from order_tracker import OrderResultTracker, OrderResult
from cache_manager import TradingCache
from captcha_utils import decode_captcha

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


def send_telegram_notification(message: str):
    """
    Send a notification to Telegram bot.
    
    Args:
        message: Message to send
    """
    try:
        # Read bot token and user ID from environment (same as simple_config_bot.py)
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        telegram_user_id = os.getenv('TELEGRAM_USER_ID') or os.getenv('USER_ID')  # Fallback for backwards compatibility
        
        if not bot_token or not telegram_user_id:
            logger.warning("Telegram credentials not found. Skipping notification.")
            logger.info("Set TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID environment variables to enable notifications.")
            return
        
        # Send message via Telegram API
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            'chat_id': telegram_user_id,
            'text': message,
            'parse_mode': 'Markdown'
        }
        
        response = requests.post(url, json=payload, timeout=10)
        
        if response.status_code == 200:
            logger.info("✓ Telegram notification sent successfully")
        else:
            logger.warning(f"Failed to send Telegram notification: {response.status_code}")
            
    except Exception as e:
        logger.error(f"Error sending Telegram notification: {e}")


# --- Order fire-log (mgmt UI reconciliation) ------------------------------
# One JSONL record per account per run is appended here, recording WHICH
# customer/broker/isin/side the bot fired an order for. The mgmt UI's
# fire_log_ingestor pulls these back over SFTP and reconciles them against the
# broker GetOrders history to authoritatively tag which executed buys were the
# bot's (vs the agent's manual trades). We emit ONCE per account in
# prepare_order_data — NOT in the place_order task, which is spammed 1000+
# times per run in the head-of-queue race. Reuses the run_results/ bind mount
# (same dir the scheduler drops its markers in).
_FIRE_LOG_SCHEMA = 1
_RUN_RESULTS_DIR = os.path.join(os.path.dirname(__file__), "run_results")
try:
    os.makedirs(_RUN_RESULTS_DIR, exist_ok=True)
except OSError:
    pass


# In-memory capture of the FIRST successful order response per account.
# Populated in the place_order HOT PATH with nothing but a dict membership
# check + one reference store (no I/O, no parse — performance is critical at
# market open), then flushed to the fire-log once in on_test_stop. Keyed by
# (username, broker_code, isin, side) -> raw response bytes.
_FIRED_SUCCESS: Dict[Any, bytes] = {}


def _extract_order_ids(resp: Any) -> tuple:
    """Best-effort ``(serial_number, tracking_number)`` from a NewOrder response.

    The serial number is the durable key we reconcile against the later
    GetOrders execution (``serialNumber``). Field locations vary by broker, so
    we probe a few names at the top level and a nested ``result``. Either may
    be ``None`` — the full response is saved regardless, so extraction can be
    refined later without a bot redeploy.
    """
    def _pick(d: dict, *names: str):
        for n in names:
            v = d.get(n)
            if v not in (None, ""):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        return None

    if not isinstance(resp, dict):
        return None, None
    nested = resp.get("result") if isinstance(resp.get("result"), dict) else {}
    serial = _pick(resp, "serialNumber", "serial", "serialNo") or _pick(
        nested, "serialNumber", "serial", "serialNo"
    )
    tracking = _pick(resp, "trackingNumber", "tracking", "trackingNo") or _pick(
        nested, "trackingNumber", "tracking", "trackingNo"
    )
    return serial, tracking


def _emit_order_fire(
    username: str,
    broker_code: str,
    isin: str,
    side: int,
    *,
    serial_number: Optional[int] = None,
    tracking_number: Optional[int] = None,
    order_response: Any = None,
) -> None:
    """Append one order-fire record to ``run_results/order_fires_<YYYYMMDD>.jsonl``.

    Records a SUCCESSFUL order placement — the broker's ``serial_number`` (the
    durable reconciliation key) plus the full response. Best-effort and never
    raises. Called once per account from on_test_stop — NOT from the
    place_order hot path. A single small JSON line in append mode (O_APPEND) is
    written atomically across locust processes. The mgmt UI dedups on
    ``fire_uid``.
    """
    try:
        now = datetime.now(timezone.utc)
        record = {
            "schema_version": _FIRE_LOG_SCHEMA,
            "fire_uid": uuid.uuid4().hex,
            "username": username,
            "broker_code": broker_code,
            "isin": isin,
            "side": side,
            "fired_at": now.isoformat(),
            "serial_number": serial_number,
            "tracking_number": tracking_number,
            "order_response": order_response,
        }
        path = os.path.join(
            _RUN_RESULTS_DIR, f"order_fires_{now.strftime('%Y%m%d')}.jsonl"
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — fire-log I/O must never break trading
        pass


def _flush_order_fires() -> None:
    """Write one fire-log line per account that placed a successful order this
    run. Parses the captured responses HERE (off the hot path) to pull the
    serial / tracking number, and saves the full response. Called once from
    on_test_stop."""
    for (username, broker_code, isin, side), content in list(_FIRED_SUCCESS.items()):
        order_resp: Any = None
        serial = tracking = None
        try:
            order_resp = json.loads(content)
            serial, tracking = _extract_order_ids(order_resp)
        except Exception:  # noqa: BLE001 — non-JSON / odd body: keep it raw
            try:
                order_resp = {"raw": content.decode("utf-8", "replace")[:4000]}
            except Exception:  # noqa: BLE001
                order_resp = None
        _emit_order_fire(
            username, broker_code, isin, side,
            serial_number=serial, tracking_number=tracking,
            order_response=order_resp,
        )
    _FIRED_SUCCESS.clear()


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

    # --- exir family (Rayan HamAfza): cookie + X-App-N adapter. The ephoenix
    # path below is left BYTE-FOR-BYTE unchanged; only non-ephoenix codes divert
    # here. Family is data-driven from the rendered config (broker_family).
    from broker_adapters import get_adapter, resolve_family
    if resolve_family(broker_code, config_section) == "exir":
        adapter = get_adapter(
            broker_code,
            username=username,
            password=password,
            config_section=config_section,
            captcha_decoder=decode_captcha,
            cache=cache_manager,
        )
        prepared = adapter.prepare_order(isin=isin, side=side, config_section=config_section)
        logger.info(
            f"✓ Exir order prepared: {username}@{broker_code} "
            f"{'Buy' if side == 1 else 'Sell'} {isin} "
            f"price={prepared.price:,} vol={prepared.volume:,}"
        )
        logger.info(f"{'='*80}\n")
        OrderData = namedtuple(
            'OrderData',
            'order_url token data username broker_code isin side api_client signer cookies',
        )
        return OrderData(
            order_url=prepared.order_url,
            token=prepared.bearer_token,
            data=prepared.body,
            username=username,
            broker_code=broker_code,
            isin=isin,
            side=side,
            api_client=None,
            signer=prepared.signer,
            cookies=prepared.cookies,
        )

    # ephoenix family — endpoints are DATA-DRIVEN from the broker code (no
    # hardcoded enum gate). A new standard ephoenix broker added in the mgmt UI
    # therefore fires here with no bot change; per-code quirks (the ib shard)
    # live in get_endpoints_for. The mgmt UI already validates the code against
    # the brokers table before it reaches config.ini, so a bad code can't get
    # here — and if one did, it just derives api-{code}.ephoenix.ir and fails at
    # the network layer rather than at a hardcoded allow-list.
    endpoints = get_endpoints_for(broker_code)

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

    OrderData = namedtuple(
        'OrderData',
        'order_url token data username broker_code isin side api_client signer cookies',
    )
    return OrderData(
        order_url=endpoints['order'],
        token=token,
        data=order_json,
        username=username,
        broker_code=broker_code,
        isin=isin,
        side=side,
        api_client=api_client,
        signer=None,    # ephoenix uses a static Bearer header (no per-request signer)
        cookies=None,
    )

# Market open timing threshold (parsed once at module level)
MARKET_OPEN_THRESHOLD = datetime.strptime('08:44:58.500', '%H:%M:%S.%f').time()
class TradingUser(HttpUser):
    """Base Locust user for trading operations."""

    abstract = True

    # Defaults so place_order's family branch is safe even if populate() isn't
    # called: ephoenix leaves these None (static Bearer header); exir overrides
    # them with a per-request X-App-N signer + the login cookies.
    signer = None
    exir_cookies = None

    def on_start(self):
        """Carry the exir login cookies onto the locust HTTP client once, off the
        hot path. No-op for ephoenix (exir_cookies is None)."""
        cookies = getattr(self, "exir_cookies", None)
        if cookies:
            for _k, _v in cookies.items():
                self.client.cookies.set(_k, _v)

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
        self.side = order_data.side
        self.api_client = order_data.api_client
        self.signer = getattr(order_data, "signer", None)
        self.exir_cookies = getattr(order_data, "cookies", None)

    @task
    def place_order(self):
        """
        Place the prepared order with the broker API.

        If the current local time is before 08:44:58.500, the task returns immediately without sending a request. Otherwise, it sends a POST request using the instance's order URL, JSON payload, and authorization token, and records the outcome to the configured logger. Exceptions raised during request submission are caught and logged.

        Performance note: this is the hot path — runs 1000+ times per dispatch
        in the head-of-queue race. Keep it lean. Diagnostic logging belongs in
        cache_warmup or prepare_order_data, not here.
        """

        # Get logger with file handler for this task
        task_logger = logging.getLogger(__name__)

        try:
            task_logger.info(f"Placing order for {self.username}@{self.broker_code}")

            if self.signer is None:
                # EPHOENIX — static Bearer header (unchanged from before the split).
                headers = {
                    "authorization": f"Bearer {self.token}",
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            else:
                # EXIR — cookie auth (carried onto self.client in on_start) plus a
                # FRESH per-request X-App-N signature (pure arithmetic, no I/O).
                headers = {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                }
                headers.update(self.signer())

            response = self.client.request(
                method="POST",
                url=self.order_url,
                name=f"{self.username}@{self.broker_code}",
                data=self.order_json,
                headers=headers
            )

            if response.status_code == 200:
                task_logger.info(f"✓ Order placed successfully for {self.username}@{self.broker_code}")
                task_logger.debug(f"Response: {response.text}")
                # Capture the FIRST successful response per account — a single
                # dict membership check + reference store. No I/O, no JSON parse
                # here (that happens once in on_test_stop). The bytes are the
                # raw body already read for this response.
                _fire_key = (self.username, self.broker_code, self.isin, self.side)
                if _fire_key not in _FIRED_SUCCESS:
                    _FIRED_SUCCESS[_fire_key] = response.content
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

    # Flush the order fire-log first (off the hot path): one record per account
    # that placed a successful order this run, carrying the broker serial number
    # + full response for the mgmt UI to reconcile against GetOrders.
    try:
        _fired_count = len(_FIRED_SUCCESS)
        _flush_order_fires()
        if _fired_count:
            logger.info(f"Order fire-log: wrote {_fired_count} successful-order record(s).")
    except Exception as e:  # noqa: BLE001 — never let fire-log flush break teardown
        logger.error(f"Order fire-log flush failed: {e}")

    total_orders = 0
    total_executed = 0
    total_volume = 0
    accounts_processed = 0
    notification_lines = []
    
    # Get all user classes
    for section_name in config.sections():
        section = dict(config[section_name])
        username = section['username']
        broker_code = section['broker']

        # Auto-sell-only sections never fire at open — nothing to summarize.
        from broker_adapters import is_auto_sell_only, resolve_family
        if is_auto_sell_only(section):
            logger.info(f"Skipping order-summary for auto-sell-only {username}@{broker_code}")
            continue

        # Exir order status arrives over WebSocket, not the ephoenix
        # GetOpenOrders feed, so this ephoenix-only post-run summary skips
        # non-ephoenix sections. The old enum path skipped them implicitly via a
        # BrokerCode ValueError; the data-driven path must skip them explicitly.
        if resolve_family(broker_code, section) != "ephoenix":
            logger.info(f"Skipping order-summary for non-ephoenix {username}@{broker_code}")
            continue

        try:
            # Get broker endpoints (data-driven from the code)
            endpoints = get_endpoints_for(broker_code)

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
            
            # Collect stats for notification
            accounts_processed += 1
            total_orders += len(orders)
            for order in orders:
                total_volume += order.volume
                if order.is_executed():
                    total_executed += 1
            
        except Exception as e:
            logger.error(f"Failed to fetch orders for {username}@{broker_code}: {e}")
            notification_lines.append(f"❌ {username}@{broker_code}: Error")
    
    logger.info("\n" + "="*80)
    logger.info("Order results saved. Check 'order_results' directory for details.")
    logger.info("="*80 + "\n")
    
    # Send Telegram notification with detailed order information
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if total_orders == 0:
            # No orders found
            notification = (
                f"📊 *Trading Completed*\n"
                f"⏰ {timestamp}\n\n"
                f"⚠️ *No Orders Found*\n\n"
                f"Accounts checked: {accounts_processed}\n\n"
                f"Possible reasons:\n"
                f"• Market is closed\n"
                f"• Orders failed to place\n"
                f"• Rate limit exceeded\n\n"
                f"Use /logs to check details"
            )
        else:
            # Orders found - include detailed information
            exec_percent = (total_executed / total_orders * 100) if total_orders > 0 else 0
            
            notification = (
                f"📊 *Trading Completed*\n"
                f"⏰ {timestamp}\n\n"
                f"✅ Orders Placed: {total_orders}\n"
                f"⚡ Executed: {total_executed}/{total_orders} ({exec_percent:.1f}%)\n"
                f"📈 Total Volume: {total_volume:,} shares\n"
                f"👥 Accounts: {accounts_processed}\n\n"
            )
            
            # Add details for each account
            account_details = []
            for section_name in config.sections():
                section = dict(config[section_name])
                username = section['username']
                broker_code = section['broker']

                # Auto-sell-only sections placed nothing this run — the mtime
                # glob below would surface a STALE result file as phantom orders.
                from broker_adapters import is_auto_sell_only
                if is_auto_sell_only(section):
                    continue

                try:
                    # Get the result file for this account
                    result_files = [f for f in Path('order_results').glob(f'*{username}_{broker_code}_*.json')]
                    if result_files:
                        latest_file = max(result_files, key=lambda f: f.stat().st_mtime)
                        with open(latest_file, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            orders = data.get('orders', [])
                            
                            if orders:
                                # Get key order details
                                order_summaries = []
                                for order in orders[:3]:  # Show up to 3 orders per account
                                    symbol = order.get('symbol', 'N/A')
                                    tracking_number = order.get('tracking_number', 'N/A')
                                    state_desc = order.get('state_desc', 'Unknown')
                                    volume = order.get('volume', 0)
                                    executed = order.get('executed_volume', 0)
                                    
                                    order_summaries.append(
                                        f"• {symbol}: {tracking_number} ({executed}/{volume}) - {state_desc}"
                                    )
                                
                                account_details.append(
                                    f"👤 *{username}@{broker_code}:*\n" + "\n".join(order_summaries)
                                )
                except Exception:
                    logger.exception(f"Error getting details for {username}@{broker_code}")
            
            if account_details:
                notification += "*Order Details:*\n\n" + "\n\n".join(account_details) + "\n\n"
            
            notification += "Use /results to view complete details"
        
        send_telegram_notification(notification)
        
    except Exception as e:
        logger.error(f"Failed to send summary notification: {e}")


# Load configuration
config = configparser.ConfigParser()
config.read('config.ini')

if not config.sections():
    logger.error("No configuration found in config.ini!")
    logger.error("Please copy config.simple.example.ini to config.ini and configure your accounts.")
    exit(1)

logger.info(f"Loaded configuration with {len(config.sections())} account(s)")

def _read_locust_users(path: str = "locust_config.json", default: int = 10) -> int:
    """Read the configured locust ``users`` from locust_config.json (the same
    file the run command builds ``-u`` from), so fixed_count tracks it."""
    try:
        import json
        with open(path, encoding="utf-8") as f:
            return int(json.load(f).get("locust", {}).get("users", default))
    except Exception:
        return default


# Dynamically create user classes for each config section
def _create_user_classes():
    """Create user classes in a function scope to avoid variable leakage to globals."""
    import sys
    from broker_adapters import is_auto_sell_only
    current_module = sys.modules[__name__]
    user_classes = []

    # Auto-sell-only sections arm the auto-sell monitor for an EXISTING holding
    # — they must NEVER fire an order at open, so they get no locust user. They
    # are also excluded from the fixed_count math below: counting watch-only
    # sections would dilute every tradeable section's share (e.g. 3 tradeable +
    # 2 watch-only at users=9 → 9//5=1 instead of 9//3=3).
    eligible = [s for s in config.sections() if not is_auto_sell_only(dict(config[s]))]
    for section_name in config.sections():
        if section_name not in eligible:
            logger.info(
                f"section {section_name}: auto_sell_only — no locust user (won't fire at open)"
            )
    if not eligible and config.sections():
        # Clean exit → green scheduled-run marker (mirrors the no-config exit(1)
        # precedent above): nothing here is supposed to fire at open.
        logger.info(
            f"all {len(config.sections())} sections are auto-sell-only — nothing to fire at open"
        )
        exit(0)

    # Pin an EQUAL number of locust users to every section via ``fixed_count``.
    # locust's default weight distribution starves some sections to 0 users when
    # the total user count is near the section count (live: a 14-section stack at
    # users=42 left one account's classes at 0 → never fired). fixed_count =
    # users // sections guarantees each section its share — no starvation.
    num_sections = len(eligible)
    _fixed_count = per_section_user_count(_read_locust_users(), num_sections)
    logger.info(
        f"locust fixed_count per section = {_fixed_count} "
        f"(configured users={_read_locust_users()}, sections={num_sections})"
    )

    for idx, section_name in enumerate(eligible, start=1):
        try:
            section = dict(config[section_name])
            
            # Prepare order data
            order_data = prepare_order_data(section)
            
            # Create unique class name with index to absolutely avoid conflicts
            base_class_name = section_name.replace('-', '_').replace('.', '_').replace(' ', '_')
            unique_class_name = f"{base_class_name}_User{idx}"
            
            # Create dynamic user class with unique name and set class attributes
            user_class = type(unique_class_name, (TradingUser,), {
                # Guarantee locust spawns an equal share for THIS section (no
                # weight-distribution starvation — see locust_scaling).
                'fixed_count': _fixed_count,
                'order_url': order_data.order_url,
                'token': order_data.token,
                'order_json': order_data.data,
                'username': order_data.username,
                'broker_code': order_data.broker_code,
                'isin': order_data.isin,
                'side': order_data.side,  # read by place_order's fire-log key
                'api_client': order_data.api_client,
                # exir per-request X-App-N signer — staticmethod so ``self.signer``
                # is NOT bound (we call it with no args). None for ephoenix.
                'signer': (
                    staticmethod(order_data.signer)
                    if order_data.signer is not None else None
                ),
                'exir_cookies': order_data.cookies,  # set onto self.client in on_start
                '__module__': __name__,  # Explicitly set module
                '__qualname__': unique_class_name,  # Explicitly set qualified name
            })
            
            # Register as module attribute using setattr instead of globals()
            setattr(current_module, unique_class_name, user_class)
            user_classes.append(unique_class_name)
            
            logger.info(f"✓ Configured trading user: {unique_class_name}")
            
        except Exception:
            logger.exception(f"✗ Failed to configure {section_name}")
    
    return user_classes

# Create all user classes
_configured_users = _create_user_classes()

logger.info("\n" + "="*80)
logger.info("All users configured. Ready to start load test.")
logger.info("="*80 + "\n")