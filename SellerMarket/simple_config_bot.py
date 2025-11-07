#!/usr/bin/env python3
"""
Simple Telegram Bot for Trading Configuration
Directly updates config.ini file
"""

import telebot
import configparser
import os
import logging
import subprocess
import json
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from typing import List, Dict, Any
import platform

# Windows-specific imports
if platform.system() == 'Windows':
    import winreg
else:
    winreg = None

import threading
from scheduler import JobScheduler

# Load environment variables from .env file
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(env_path):
    load_dotenv(env_path)

# Global dict to track running background processes
running_processes = {}
process_lock = threading.Lock()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
USER_ID = os.getenv('TELEGRAM_USER_ID')
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.ini')
SCHEDULER_CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'scheduler_config.json')
RESULTS_DIR = os.path.join(os.path.dirname(__file__), 'order_results')
LOG_FILE = os.path.join(os.path.dirname(__file__), 'trading_bot.log')

# Validate environment variables only when running the bot (not when importing for tests)
def validate_environment():
    """
    Ensure required environment variables for the Telegram bot are present.
    
    Raises:
        ValueError: if `TELEGRAM_BOT_TOKEN` is not set or if `TELEGRAM_USER_ID` is not set.
    """
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")
    if not USER_ID:
        raise ValueError("TELEGRAM_USER_ID not set")

# Ensure environment variables are set for subprocesses
# Locustfile expects TELEGRAM_BOT_TOKEN and USER_ID
if BOT_TOKEN:
    os.environ['TELEGRAM_BOT_TOKEN'] = BOT_TOKEN
if USER_ID:
    os.environ['USER_ID'] = USER_ID
    os.environ['TELEGRAM_USER_ID'] = USER_ID  # Keep both for compatibility

# Configure telebot to use requests session with proxy auto-detection
# This is necessary for Windows services which don't inherit user proxy settings
def get_windows_proxy():
    """
    Retrieve Windows Internet proxy settings from the registry.
    
    Reads the current user's Internet Settings registry key and, if a system proxy is enabled,
    returns a dictionary formatted for use with HTTP client libraries (e.g., requests).
    The dictionary will either contain protocol-specific mappings (e.g., {'http': 'http://host:port', 'https': 'http://host:port'})
    or protocol keys parsed from a protocol-specific ProxyServer value (e.g., {'http': 'http://host:port', 'ftp': 'http://host:port'}).
    
    Returns:
        dict or None: A proxies dictionary suitable for passing to HTTP clients when a proxy is configured;
        `None` if not running on Windows, no proxy is enabled, or the proxy settings cannot be read.
    """
    if winreg is None:
        return None  # Not on Windows
    
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            proxy_enable, _ = winreg.QueryValueEx(key, 'ProxyEnable')
            if proxy_enable:
                proxy_server, _ = winreg.QueryValueEx(key, 'ProxyServer')
                logger.info(f"Found Windows proxy: {proxy_server}")
                # Format as dict for requests
                if '=' in proxy_server:
                    # Protocol-specific proxies
                    proxies = {}
                    for item in proxy_server.split(';'):
                        protocol, address = item.split('=', 1)
                        proxies[protocol] = f'http://{address}'
                    return proxies
                else:
                    # Single proxy for all protocols
                    return {
                        'http': f'http://{proxy_server}',
                        'https': f'http://{proxy_server}'
                    }
    except Exception as e:
        logger.info(f"No Windows proxy configured: {e}")
    return None

# Set proxy in telebot if found
import telebot.apihelper
proxy_config = get_windows_proxy()
if proxy_config:
    telebot.apihelper.proxy = proxy_config
    logger.info(f"Telegram bot configured with proxy: {proxy_config}")
else:
    logger.info("No proxy configured - using direct connection")

# Initialize bot - use dummy token if BOT_TOKEN not set (for tests)
# Tests should not trigger bot initialization
if BOT_TOKEN:
    bot = telebot.TeleBot(BOT_TOKEN)
else:
    # For tests - use a properly formatted dummy token
    bot = telebot.TeleBot("123456789:ABCdefGHIjklMNOpqrsTUVwxyz")

# Initialize scheduler
scheduler = JobScheduler(SCHEDULER_CONFIG_FILE)

def is_authorized(message):
    """
    Verify that the incoming Telegram message originates from the configured authorized user.
    
    If the sender does not match USER_ID, replies to the message with "❌ Unauthorized" and returns False.
    
    Parameters:
        message: Telegram message object to check (expects a `from_user.id` attribute).
    
    Returns:
        `True` if the message sender matches `USER_ID`, `False` otherwise.
    """
    if USER_ID and str(message.from_user.id) != str(USER_ID):
        bot.reply_to(message, "❌ Unauthorized")
        return False
    return True

def read_config():
    """
    Load and return the application's INI configuration.
    
    Reads CONFIG_FILE and returns a populated configparser.ConfigParser. If the file does not exist or is empty, an empty ConfigParser is returned.
     
    Returns:
        config (configparser.ConfigParser): Parsed configuration for CONFIG_FILE.
    """
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE, encoding='utf-8')
    return config

def save_config(config):
    """
    Persist the provided ConfigParser to the module's CONFIG_FILE (config.ini).
    
    Parameters:
        config (configparser.ConfigParser): Configuration object whose contents will be written to the config file.
    """
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        config.write(f)
    logger.info("Configuration saved")

def get_latest_result_file() -> str:
    """
    Return the path to the most recently modified JSON result file.
    
    Searches the RESULTS_DIR for files with a .json extension and selects the one with the latest modification time.
    
    Returns:
    	str or None: Path to the latest JSON result file as a string, or `None` if the results directory does not exist, no JSON files are found, or an error occurs.
    """
    try:
        if not os.path.exists(RESULTS_DIR):
            return None
        
        files = [f for f in Path(RESULTS_DIR).glob('*.json')]
        if not files:
            return None
        
        # Sort by modification time, most recent first
        latest = max(files, key=lambda f: f.stat().st_mtime)
        return str(latest)
    except Exception as e:
        logger.error(f"Error finding latest result: {e}")
        return None

def format_order_results(result_file: str) -> str:
    """
    Create a Telegram-formatted summary of trading results from a JSON result file.
    
    Reads the specified JSON file and builds a message that includes account and broker, timestamp, a summary (number of orders, total volume, executed volume with percentage, and total amount), and a listing of up to the first five orders with side, symbol, volume, price, and status. If no orders are present, returns a short "No orders found" message. On error, returns an error string describing the failure.
    
    Parameters:
        result_file (str): Path to the JSON file containing trading results.
    
    Returns:
        str: A Markdown-formatted message suitable for Telegram containing the results summary and order details, or an error message if reading or formatting fails.
    """
    try:
        with open(result_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        username = data.get('username', 'Unknown')
        broker = data.get('broker_code', 'Unknown')
        timestamp = data.get('timestamp', '')
        orders = data.get('orders', [])
        
        if not orders:
            return f"📊 *Trading Results* [{username}@{broker}]\n\n⚠️ No orders found"
        
        # Calculate summary
        total_volume = sum(o.get('volume', 0) for o in orders)
        total_executed = sum(o.get('executed_volume', 0) for o in orders)
        total_amount = sum(o.get('net_amount', 0) for o in orders)
        
        # Format message
        msg = f"📊 *Trading Results*\n\n"
        msg += f"👤 Account: `{username}@{broker}`\n"
        msg += f"🕐 Time: {datetime.fromisoformat(timestamp).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        msg += f"📈 *Summary:*\n"
        msg += f"  Orders: {len(orders)}\n"
        msg += f"  Volume: {total_volume:,} shares\n"
        msg += f"  Executed: {total_executed:,} ({total_executed/total_volume*100:.1f}%)\n" if total_volume > 0 else "  Executed: 0\n"
        msg += f"  Amount: {total_amount:,.0f} Rials\n\n"
        
        # Show first 5 orders
        msg += f"📋 *Orders:* (showing first 5)\n"
        for i, order in enumerate(orders[:5], 1):
            side = "BUY" if order.get('side') == 1 else "SELL"
            symbol = order.get('symbol', 'N/A')
            volume = order.get('volume', 0)
            price = order.get('price', 0)
            state = order.get('state_desc', 'Unknown')
            
            msg += f"{i}. {side} {volume:,} × {symbol} @ {price:,}\n"
            msg += f"   Status: {state}\n"
        
        if len(orders) > 5:
            msg += f"\n... and {len(orders) - 5} more orders\n"
        
        return msg
        
    except Exception as e:
        logger.error(f"Error formatting results: {e}")
        return f"❌ Error reading results: {str(e)}"

def get_log_tail(lines: int = 50) -> str:
    """
    Return a Telegram-formatted string containing the last `lines` lines of the log file.
    
    Parameters:
        lines (int): Number of lines from the end of the log to include (default 50).
    
    Returns:
        str: A message formatted for Telegram containing:
            - a header with the count of returned lines and the filename,
            - the log tail inside a Markdown code block,
            - a truncated view if the log content exceeds ~3800 characters,
            - or a short informational/error message if the log file is missing, empty, or unreadable.
    """
    try:
        if not os.path.exists(LOG_FILE):
            return "📝 No log file found"
        
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
        
        if not all_lines:
            return "📝 Log file is empty"
        
        # Get last N lines
        tail_lines = all_lines[-lines:]
        
        # Format for Telegram
        log_text = ''.join(tail_lines)
        
        # Truncate if too long (Telegram limit is 4096 chars)
        if len(log_text) > 3800:
            log_text = log_text[-3800:]
            log_text = "...[truncated]\n" + log_text
        
        return f"📝 *Last {len(tail_lines)} lines of trading_bot.log:*\n\n```\n{log_text}\n```"
        
    except Exception as e:
        logger.error(f"Error reading log: {e}")
        return f"❌ Error reading log: {str(e)}"

def get_active_section(config):
    """
    Get the first non-commented section name from the given ConfigParser.
    
    Parameters:
        config (configparser.ConfigParser): Parsed INI configuration to inspect.
    
    Returns:
        str or None: The name of the first section that does not start with `#` or `;`, or `None` if no such section exists.
    """
    for section in config.sections():
        if not section.startswith('#') and not section.startswith(';'):
            return section
    return None

def set_active_section(config_file, section_name):
    """
    Activate a named INI section by uncommenting its keys and commenting out all other sections.
    
    Parameters:
        config_file (str): Path to the INI file to modify.
        section_name (str): The section name to activate.
    
    Description:
        The function rewrites the file so that the specified section header is uncommented (e.g. "[section]")
        and all other section headers are commented (prefixed with ';'). Within the activated section,
        any key lines that were commented are uncommented. The file is updated in place.
    
    """
    with open(config_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    new_lines = []
    in_target_section = False
    
    for line in lines:
        stripped = line.strip()
        
        # Check if this is a section header
        if stripped.startswith('[') and stripped.endswith(']'):
            # Extract section name (remove comments and brackets)
            current_section = stripped.lstrip('#;').strip('[]')
            
            if current_section == section_name:
                # Uncomment target section
                new_lines.append(f'[{section_name}]\n')
                in_target_section = True
            else:
                # Comment out other sections
                if not stripped.startswith('#') and not stripped.startswith(';'):
                    new_lines.append(f'; [{current_section}]\n')
                else:
                    new_lines.append(line)
                in_target_section = False
        else:
            # For non-section lines, uncomment if in target section
            if in_target_section and (stripped.startswith(';') or stripped.startswith('#')):
                # Remove comment prefix
                uncommented = stripped.lstrip('#;').strip()
                if '=' in uncommented:
                    new_lines.append(uncommented + '\n')
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)
    
    with open(config_file, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    
    logger.info(f"Set active section to: {section_name}")

@bot.message_handler(commands=['list'])
def list_configs(message):
    """
    Send a Telegram reply listing all configuration sections and indicating which one is active.
    
    Reads the configuration file (including commented section headers), formats each section with an active marker, and replies to the user with the list and guidance to switch using `/use <name>`. Requires the sender to be authorized; on error sends an error reply and logs the failure.
    
    Parameters:
        message: The Telegram message object that triggered the command; used to determine the sender and to send the reply.
    """
    if not is_authorized(message):
        return
    
    try:
        config = read_config()
        sections = []
        active_section = get_active_section(config)
        
        # Get all sections including commented ones
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('[') and ']' in stripped:
                    # Extract section name, handle comments on same line
                    bracket_end = stripped.index(']')
                    section_part = stripped[:bracket_end + 1]
                    section_name = section_part.lstrip('#;[').rstrip(']').strip()
                    
                    if section_name:  # Skip empty section names
                        is_active = (section_name == active_section)
                        status = "✅ ACTIVE" if is_active else "⚪"
                        sections.append(f"{status} `{section_name}`")
        
        if not sections:
            bot.reply_to(message, "📝 No configurations found\n\nUse /add <name> to create one")
            return
        
        response = "📋 *Available Configs:*\n\n" + "\n".join(sections)
        response += "\n\nUse `/use <name>` to switch"
        
        bot.reply_to(message, response, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error listing configs: {e}")
        bot.reply_to(message, "❌ Error listing configurations")

@bot.message_handler(commands=['use'])
def use_config(message):
    """
    Switch the bot's active configuration to the section named in the message.
    
    Expects the message text to be "/use <config_name>" and replies to the sender with success or error feedback. Requires an authorized user; if the named section exists in the config file (including commented sections), that section is made active in the config by uncommenting it and commenting other sections. If the section is missing or an error occurs, an explanatory reply is sent.
     
    Parameters:
        message: Telegram message object containing the command text (e.g., "/use my_config").
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /use <config_name>\n\nUse /list to see available configs")
            return
        
        target_section = parts[1]
        
        # Check if section exists
        config = read_config()
        all_sections = []
        
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('[') and ']' in stripped:
                    bracket_end = stripped.index(']')
                    section_part = stripped[:bracket_end + 1]
                    section_name = section_part.lstrip('#;[').rstrip(']').strip()
                    if section_name:
                        all_sections.append(section_name)
        
        if target_section not in all_sections:
            available = ', '.join([f'`{s}`' for s in all_sections])
            bot.reply_to(message, f"❌ Config `{target_section}` not found\n\nAvailable: {available}", parse_mode='Markdown')
            return
        
        set_active_section(CONFIG_FILE, target_section)
        bot.reply_to(message, f"✅ Switched to config: `{target_section}`", parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error switching config: {e}")
        bot.reply_to(message, "❌ Error switching configuration")

@bot.message_handler(commands=['add'])
def add_config(message):
    """
    Create a new configuration section in the INI file and append default keys.
    
    Adds a new section named by the first argument of the incoming Telegram command to CONFIG_FILE with default keys: username, password, broker, isin, and side. If the named section already exists (commented or uncommented), the function replies that the config exists. On success it confirms creation and suggests how to activate the new section. On error it logs and notifies the user.
    
    Parameters:
    	message: Telegram message object containing the `/add <config_name>` command; the second token is used as the new section name.
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /add <config_name>\n\nExample: /add Account2")
            return
        
        new_section = parts[1]
        
        # Check if section already exists
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
            if f'[{new_section}]' in content or f'; [{new_section}]' in content:
                bot.reply_to(message, f"❌ Config `{new_section}` already exists", parse_mode='Markdown')
                return
        
        # Add new section at the end
        with open(CONFIG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'\n[{new_section}]\n')
            f.write('username = \n')
            f.write('password = \n')
            f.write('broker = gs\n')
            f.write('isin = IRO1MHRN0001\n')
            f.write('side = 1\n')
        
        logger.info(f"Added new config: {new_section}")
        bot.reply_to(message, f"✅ Created config: `{new_section}`\n\nUse `/use {new_section}` to switch to it", parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error adding config: {e}")
        bot.reply_to(message, "❌ Error adding configuration")

@bot.message_handler(commands=['remove'])
def remove_config(message):
    """
    Remove a named configuration section from the persistent config file and notify the user.
    
    Expects the incoming Telegram `message` to contain the command and the target config name (e.g. `/remove myconfig`). If the section exists it is removed from CONFIG_FILE and the bot replies with a confirmation; on error or if the section is missing the bot replies with an appropriate error or usage message. This action requires an authorized user and logs the removal or any errors.
     
    Parameters:
        message (telebot.types.Message): Telegram message invoking the command with the config name.
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /remove <config_name>\n\nUse /list to see available configs")
            return
        
        target_section = parts[1]
        
        # Read all lines
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Find and remove the section
        new_lines = []
        in_target_section = False
        section_found = False
        
        for line in lines:
            stripped = line.strip()
            
            if stripped.startswith('[') and stripped.endswith(']'):
                section_name = stripped.lstrip('#;[').rstrip(']').strip()
                
                if section_name == target_section:
                    in_target_section = True
                    section_found = True
                    continue  # Skip this line
                else:
                    in_target_section = False
            
            if not in_target_section:
                new_lines.append(line)
        
        if not section_found:
            bot.reply_to(message, f"❌ Config `{target_section}` not found", parse_mode='Markdown')
            return
        
        # Write back
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        
        logger.info(f"Removed config: {target_section}")
        bot.reply_to(message, f"✅ Removed config: `{target_section}`", parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error removing config: {e}")
        bot.reply_to(message, "❌ Error removing configuration")

@bot.message_handler(commands=['start', 'help'])
def send_help(message):
    """
    Send the bot's instructional help text to the requesting user.
    
    The message lists available commands for configuration management, manual execution, scheduler management, and examples. This handler requires the sender to be authorized and replies using Markdown formatting.
    
    Parameters:
        message (telebot.types.Message): Incoming Telegram message to which the help text will be replied.
    """
    if not is_authorized(message):
        return
    
    help_text = """
🤖 *Trading Bot Config*

*Config Management:*
/list - List all configs
/use <name> - Switch to config
/add <name> - Add new config
/remove <name> - Remove config
/show - Show current config

*Update Current Config:*
/broker <code> - Set broker
/symbol <ISIN> - Set stock symbol
/side <1|2> - Set side (1=Buy, 2=Sell)
/user <username> - Set username
/pass <password> - Set password

*Manual Execution:*
/cache - Run cache warmup now
/trade - Run trading bot now
/stop - Stop all running processes
/status - Show system status
/results - Show latest trading results
/logs [lines] - Show recent logs (default: 50)

*Scheduler Management:*
/schedule - Show scheduled jobs
/setcache <HH:MM:SS> - Set cache time
/settrade <HH:MM:SS> - Set trade time
/enablejob <name> - Enable job
/disablejob <name> - Disable job

*Example:*
/list
/add Account2
/use Account2
/broker bbi
/cache
/trade
/setcache 08:30:00
/settrade 08:44:30
"""
    bot.reply_to(message, help_text, parse_mode='Markdown')

@bot.message_handler(commands=['show'])
def show_config(message):
    """
    Send the currently active configuration to the user as a formatted Telegram message.
    
    Replies to the invoking message with the active INI section name and its fields: username, masked password (asterisks), broker, symbol (isin), and side (displayed as "Buy" for '1' and "Sell" otherwise). If no active section exists or a read error occurs, sends an appropriate error reply.
    
    Parameters:
        message: The Telegram message object that triggered the command; used to reply to the user.
    """
    if not is_authorized(message):
        return
    
    try:
        config = read_config()
        section = get_active_section(config)
        
        if not section:
            bot.reply_to(message, "❌ No active configuration found")
            return
        
        cfg = config[section]
        response = f"""
📋 *Current Config* [{section}]

👤 Username: `{cfg.get('username', 'Not set')}`
🔑 Password: `{'*' * len(cfg.get('password', ''))}` 
🏛️ Broker: `{cfg.get('broker', 'Not set')}`
📈 Symbol: `{cfg.get('isin', 'Not set')}`
📊 Side: `{cfg.get('side', 'Not set')}` ({'Buy' if cfg.get('side') == '1' else 'Sell'})
"""
        bot.reply_to(message, response, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error reading config: {e}")
        bot.reply_to(message, "❌ Error reading configuration")

@bot.message_handler(commands=['broker'])
def set_broker(message):
    """
    Set the active configuration's broker code.
    
    Updates the currently active INI section's `broker` value based on the broker code provided in the incoming Telegram message (e.g. "/broker gs"). Valid broker codes are: gs, bbi, shahr, karamad, tejarat, shams. Replies to the user with confirmation or an error message if the input is invalid or no active configuration exists.
    
    Parameters:
        message: The Telegram message object containing the command text (expected format: "/broker <code>").
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /broker <code>\nExample: /broker gs")
            return
        
        broker = parts[1].lower()
        valid_brokers = ['gs', 'bbi', 'shahr', 'karamad', 'tejarat', 'shams']
        
        if broker not in valid_brokers:
            bot.reply_to(message, f"❌ Invalid broker. Valid: {', '.join(valid_brokers)}")
            return
        
        config = read_config()
        section = get_active_section(config)
        
        if not section:
            bot.reply_to(message, "❌ No active configuration found")
            return
        
        config[section]['broker'] = broker
        save_config(config)
        
        broker_names = {
            'gs': 'Ganjine',
            'bbi': 'Bourse Bazar Iran',
            'shahr': 'Shahr',
            'karamad': 'Karamad',
            'tejarat': 'Tejarat',
            'shams': 'Shams'
        }
        
        bot.reply_to(message, f"✅ Broker set to: *{broker_names.get(broker, broker)}*", parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error setting broker: {e}")
        bot.reply_to(message, "❌ Error updating broker")

@bot.message_handler(commands=['symbol'])
def set_symbol(message):
    """
    Update the active configuration's ISIN (stock symbol) to the value provided in the command.
    
    If no active configuration exists or the command format is invalid, the function sends an explanatory reply. On success it sends a confirmation message showing the new ISIN.
    
    Parameters:
        message: Telegram message object containing the command text in the form "/symbol <ISIN>" (e.g., "/symbol IRO1MHRN0001").
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /symbol <ISIN>\nExample: /symbol IRO1MHRN0001")
            return
        
        symbol = parts[1].upper()
        
        config = read_config()
        section = get_active_section(config)
        
        if not section:
            bot.reply_to(message, "❌ No active configuration found")
            return
        
        config[section]['isin'] = symbol
        save_config(config)
        
        bot.reply_to(message, f"✅ Symbol set to: `{symbol}`", parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error setting symbol: {e}")
        bot.reply_to(message, "❌ Error updating symbol")

@bot.message_handler(commands=['side'])
def set_side(message):
    """
    Set the active configuration's trade side to buy or sell based on the command argument.
    
    Updates the active config section's `side` value to '1' (BUY) or '2' (SELL), persists the change to the config file, and sends a confirmation or error message back to the user via the bot. The command expects the message text in the form "/side <1|2>".
    
    Parameters:
        message: Telegram message object whose `text` should contain the command and a side argument ("/side 1" or "/side 2").
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /side <1|2>\n1 = Buy\n2 = Sell")
            return
        
        side = parts[1]
        if side not in ['1', '2']:
            bot.reply_to(message, "❌ Side must be 1 (Buy) or 2 (Sell)")
            return
        
        config = read_config()
        section = get_active_section(config)
        
        if not section:
            bot.reply_to(message, "❌ No active configuration found")
            return
        
        config[section]['side'] = side
        save_config(config)
        
        side_name = 'BUY' if side == '1' else 'SELL'
        bot.reply_to(message, f"✅ Side set to: *{side_name}*", parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error setting side: {e}")
        bot.reply_to(message, "❌ Error updating side")

@bot.message_handler(commands=['user'])
def set_username(message):
    """
    Update the active configuration's `username` value from a Telegram /user command and confirm the change.
    
    Expects `message.text` to contain "/user <username>". If an active config section exists, writes the new username to the config file and sends a confirmation message. Attempts to delete the user's original message containing the username for security; if deletion fails, the update still proceeds. Replies with usage guidance or an error message on failure.
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /user <username>")
            return
        
        username = parts[1]
        
        config = read_config()
        section = get_active_section(config)
        
        if not section:
            bot.reply_to(message, "❌ No active configuration found")
            return
        
        config[section]['username'] = username
        save_config(config)
        
        # Delete the message containing username for security
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        
        bot.send_message(message.chat.id, "✅ Username updated (message deleted for security)")
        
    except Exception as e:
        logger.error(f"Error setting username: {e}")
        bot.reply_to(message, "❌ Error updating username")

@bot.message_handler(commands=['pass'])
def set_password(message):
    """
    Update the active configuration's password from a Telegram command and confirm the change.
    
    Reads the password argument from the incoming `message`, sets it in the currently active INI section, persists the configuration, attempts to delete the user's message containing the password for security, and replies with success or error feedback.
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /pass <password>")
            return
        
        password = parts[1]
        
        config = read_config()
        section = get_active_section(config)
        
        if not section:
            bot.reply_to(message, "❌ No active configuration found")
            return
        
        config[section]['password'] = password
        save_config(config)
        
        # Delete the message containing password for security
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        
        bot.send_message(message.chat.id, "✅ Password updated (message deleted for security)")
        
    except Exception as e:
        logger.error(f"Error setting password: {e}")
        bot.reply_to(message, "❌ Error updating password")

# ========================================
# Manual Execution Commands
# ========================================

@bot.message_handler(commands=['cache'])
def run_cache_warmup(message):
    """
    Trigger a manual cache warmup and report progress and results to the user.
    
    Runs the cache warmup script and sends Telegram replies indicating start, success, failure, or timeout.
    On success or failure the function includes up to the last 1000 characters of the subprocess output or error in the reply.
    If the operation exceeds the configured timeout (5 minutes), a timeout notice is sent and the warmup may continue running in the background.
    
    Parameters:
        message: Telegram message object used to reply to the requesting user.
    """
    if not is_authorized(message):
        return
    
    try:
        bot.reply_to(message, "🔄 Running cache warmup...\nThis may take 2-5 minutes depending on number of accounts")
        
        result = subprocess.run(
            ['python', 'cache_warmup.py'],
            cwd=os.path.dirname(__file__),
            capture_output=True,
            text=True,
            timeout=300,  # 5 minutes timeout
            env=os.environ.copy()  # Pass environment variables to subprocess
        )
        
        if result.returncode == 0:
            # Get last 1000 characters of output for better visibility
            output = result.stdout[-1000:] if result.stdout else "No output"
            bot.reply_to(message, f"✅ Cache warmup completed successfully!\n\n```\n{output}\n```", parse_mode='Markdown')
        else:
            error = result.stderr[-1000:] if result.stderr else "Unknown error"
            bot.reply_to(message, f"❌ Cache warmup failed!\n\n```\n{error}\n```", parse_mode='Markdown')
    
    except subprocess.TimeoutExpired:
        bot.reply_to(message, "⏱️ Cache warmup is taking longer than expected (>5 minutes).\n\nIt may still be running in the background. Check logs with /logs command.")
    except Exception as e:
        logger.error(f"Error running cache warmup: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['trade'])
def run_trading(message):
    """
    Manually triggers a short, headless trading run using Locust and reports the result to the sender.
    
    This handler validates the sender, starts a Locust subprocess with a preset configuration (10 users, 30s run), captures its output, and sends a concise summary or error message back to the Telegram message thread. Timeouts and subprocess errors are handled and reported to the user.
    
    Parameters:
        message: Telegram message object used for authorization and to send reply messages.
    """
    if not is_authorized(message):
        return
    
    try:
        bot.reply_to(message, "🚀 Starting trading bot...\nDefault: 10 users, 30 seconds run time")
        
        result = subprocess.run(
            ['locust', '-f', 'locustfile_new.py', '--headless', '--users', '10', '--spawn-rate', '10', '--run-time', '30s', '--host', 'https://abc.com'],
            cwd=os.path.dirname(__file__),
            capture_output=True,
            text=True,
            timeout=120,  # 2 minutes timeout for trading
            env=os.environ.copy()  # Pass environment variables to subprocess
        )
        
        if result.returncode == 0:
            # Extract summary from output
            output_lines = result.stdout.split('\n')
            summary = '\n'.join([line for line in output_lines if 'RPS' in line or 'requests' in line or 'Aggregated' in line])
            
            bot.reply_to(message, f"✅ Trading completed!\n\n```\n{summary[-1000:]}\n```", parse_mode='Markdown')
        else:
            error = result.stderr[-1000:] if result.stderr else "Unknown error"
            bot.reply_to(message, f"❌ Trading failed!\n\n```\n{error}\n```", parse_mode='Markdown')
    
    except subprocess.TimeoutExpired:
        bot.reply_to(message, "⏱️ Trading is taking longer than expected (>2 minutes).\n\nIt may still be running. Check /logs for details.")
    except Exception as e:
        logger.error(f"Error running trading: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['status'])
def show_status(message):
    """
    Send a concise system status report to the user and reply with cached service and scheduler information.
    
    Replies to the provided Telegram message with a summary of cache health, configured scheduled jobs (if any), and basic service/bot state. If an error occurs while gathering status, sends an error message back to the user.
    
    Parameters:
        message: The incoming Telegram message object used to validate authorization and send the reply.
    """
    if not is_authorized(message):
        return
    
    try:
        # Check cache status
        cache_result = subprocess.run(
            ['python', 'cache_cli.py', 'stats'],
            cwd=os.path.dirname(__file__),
            capture_output=True,
            text=True,
            timeout=10
        )
        
        cache_status = cache_result.stdout if cache_result.returncode == 0 else "❌ Cache unavailable"
        
        # Check scheduler config
        scheduler_status = "📅 Not configured"
        if os.path.exists(SCHEDULER_CONFIG_FILE):
            with open(SCHEDULER_CONFIG_FILE, 'r') as f:
                scheduler_config = json.load(f)
                enabled = scheduler_config.get('enabled', False)
                jobs = scheduler_config.get('jobs', [])
                
                if enabled and jobs:
                    job_status = []
                    for job in jobs:
                        status_icon = "✅" if job.get('enabled') else "⚪"
                        job_status.append(f"{status_icon} {job['name']}: {job['time']}")
                    scheduler_status = "📅 *Scheduled Jobs:*\n" + "\n".join(job_status)
        
        response = f"""
📊 *System Status*

{cache_status[:300]}

{scheduler_status}

💻 *Service:* Running
🤖 *Bot:* Active
"""
        bot.reply_to(message, response, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

# ========================================
# Scheduler Management Commands
# ========================================

@bot.message_handler(commands=['schedule'])
def show_schedule(message):
    """
    Display the configured scheduler and its jobs to the requesting Telegram user.
    
    Reads the scheduler configuration file and replies to the provided Telegram message with a formatted list of scheduled jobs, showing each job's enabled state, name, scheduled time, and a truncated command. If no scheduler configuration or no jobs are found, replies with guidance on how to configure scheduling. On error, logs the exception and sends an error reply.
    
    Parameters:
        message: The incoming Telegram message object to which the bot will send the reply.
    """
    if not is_authorized(message):
        return
    
    try:
        if not os.path.exists(SCHEDULER_CONFIG_FILE):
            bot.reply_to(message, "📅 No scheduler configuration found\n\nUse /setcache and /settrade to configure")
            return
        
        with open(SCHEDULER_CONFIG_FILE, 'r') as f:
            config = json.load(f)
        
        enabled = config.get('enabled', True)
        jobs = config.get('jobs', [])
        
        if not jobs:
            bot.reply_to(message, "📅 No scheduled jobs configured")
            return
        
        response = f"📅 *Scheduled Jobs* ({'Enabled' if enabled else 'Disabled'})\n\n"
        
        for job in jobs:
            status_icon = "✅" if job.get('enabled') else "⚪"
            response += f"{status_icon} *{job['name']}*\n"
            response += f"   ⏰ Time: `{job['time']}`\n"
            response += f"   📝 Command: `{job['command'][:50]}...`\n\n"
        
        response += "\n*Management:*\n"
        response += "/setcache <HH:MM:SS>\n"
        response += "/settrade <HH:MM:SS>\n"
        response += "/enablejob <name>\n"
        response += "/disablejob <name>"
        
        bot.reply_to(message, response, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error showing schedule: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['setcache'])
def set_cache_time(message):
    """
    Schedule or update the cache warmup job time in the scheduler configuration.
    
    Validates the provided time (HH:MM:SS), adds or updates a job named "cache_warmup" in the scheduler config file, saves the change, and reloads the scheduler so the new time takes effect. Replies to the originating Telegram message with a confirmation on success or an error message on failure.
    
    Parameters:
        message: Telegram message object containing the command text; the expected format is "/setcache HH:MM:SS" where the second token is the target time.
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /setcache HH:MM:SS\n\nExample: /setcache 08:30:00")
            return
        
        time_str = parts[1]
        
        # Validate time format
        datetime.strptime(time_str, '%H:%M:%S')
        
        # Load or create config
        if os.path.exists(SCHEDULER_CONFIG_FILE):
            with open(SCHEDULER_CONFIG_FILE, 'r') as f:
                config = json.load(f)
        else:
            config = {"enabled": True, "jobs": []}
        
        # Update or add cache job
        found = False
        for job in config['jobs']:
            if job['name'] == 'cache_warmup':
                job['time'] = time_str
                found = True
                break
        
        if not found:
            config['jobs'].append({
                "name": "cache_warmup",
                "time": time_str,
                "command": "python cache_warmup.py",
                "enabled": True
            })
        
        # Save config
        with open(SCHEDULER_CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        
        # Reload scheduler to apply changes immediately
        scheduler.reload_config()
        
        bot.reply_to(message, f"✅ Cache warmup scheduled for: `{time_str}`", parse_mode='Markdown')
        
    except ValueError:
        bot.reply_to(message, "❌ Invalid time format. Use HH:MM:SS (e.g., 08:30:00)")
    except Exception as e:
        logger.error(f"Error setting cache time: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['settrade'])
def set_trade_time(message):
    """
    Schedule or update the trading job time from a `/settrade HH:MM:SS` command message.
    
    Validates that the provided time is in HH:MM:SS format, updates or adds a `run_trading` job in the scheduler configuration file, saves the file, reloads the scheduler to apply changes immediately, and replies to the user with a confirmation or error message.
    
    Parameters:
        message: Telegram message object containing the `/settrade` command and target time (e.g., `/settrade 08:44:30`).
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /settrade HH:MM:SS\n\nExample: /settrade 08:44:30")
            return
        
        time_str = parts[1]
        
        # Validate time format
        datetime.strptime(time_str, '%H:%M:%S')
        
        # Load or create config
        if os.path.exists(SCHEDULER_CONFIG_FILE):
            with open(SCHEDULER_CONFIG_FILE, 'r') as f:
                config = json.load(f)
        else:
            config = {"enabled": True, "jobs": []}
        
        # Update or add trade job
        found = False
        for job in config['jobs']:
            if job['name'] == 'run_trading':
                job['time'] = time_str
                found = True
                break
        
        if not found:
            config['jobs'].append({
                "name": "run_trading",
                "time": time_str,
                "command": "locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 30s",
                "enabled": True
            })
        
        # Save config
        with open(SCHEDULER_CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        
        # Reload scheduler to apply changes immediately
        scheduler.reload_config()
        
        bot.reply_to(message, f"✅ Trading scheduled for: `{time_str}`", parse_mode='Markdown')
        
    except ValueError:
        bot.reply_to(message, "❌ Invalid time format. Use HH:MM:SS (e.g., 08:44:30)")
    except Exception as e:
        logger.error(f"Error setting trade time: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['enablejob'])
def enable_job(message):
    """
    Enable a scheduled job by name and notify the user of the result.
    
    This handler expects the incoming Telegram message to contain the command and a job name (usage: `/enablejob <job_name>`). It sets the named job's `enabled` flag to `true` in the scheduler configuration file, persists the change, reloads the scheduler so changes take effect immediately, and replies to the sender with success or error messages. If the scheduler configuration file or the named job is missing, an explanatory reply is sent.
    
    Parameters:
        message: Incoming Telegram message containing the `/enablejob` command and the target job name.
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /enablejob <job_name>\n\nExample: /enablejob cache_warmup")
            return
        
        job_name = parts[1]
        
        if not os.path.exists(SCHEDULER_CONFIG_FILE):
            bot.reply_to(message, "❌ No scheduler configuration found")
            return
        
        with open(SCHEDULER_CONFIG_FILE, 'r') as f:
            config = json.load(f)
        
        found = False
        for job in config.get('jobs', []):
            if job['name'] == job_name:
                job['enabled'] = True
                found = True
                break
        
        if not found:
            bot.reply_to(message, f"❌ Job `{job_name}` not found", parse_mode='Markdown')
            return
        
        with open(SCHEDULER_CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        
        # Reload scheduler to apply changes immediately
        scheduler.reload_config()
        
        bot.reply_to(message, f"✅ Job `{job_name}` enabled", parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error enabling job: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['disablejob'])
def disable_job(message):
    """
    Disable a scheduler job specified by name from the incoming Telegram command.
    
    Parses the job name from message.text (usage: /disablejob <job_name>), sets that job's "enabled" field to False in the scheduler configuration file, writes the updated config, reloads the in-process scheduler to apply changes immediately, and replies to the user with a confirmation or error message. Requires an authorized user; if the scheduler config or the named job is missing, a user-facing error reply is sent.
    
    Parameters:
        message (telebot.types.Message): Telegram message containing the command and job name (e.g. "/disablejob cache_warmup").
    
    Returns:
        None
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /disablejob <job_name>\n\nExample: /disablejob cache_warmup")
            return
        
        job_name = parts[1]
        
        if not os.path.exists(SCHEDULER_CONFIG_FILE):
            bot.reply_to(message, "❌ No scheduler configuration found")
            return
        
        with open(SCHEDULER_CONFIG_FILE, 'r') as f:
            config = json.load(f)
        
        found = False
        for job in config.get('jobs', []):
            if job['name'] == job_name:
                job['enabled'] = False
                found = True
                break
        
        if not found:
            bot.reply_to(message, f"❌ Job `{job_name}` not found", parse_mode='Markdown')
            return
        
        with open(SCHEDULER_CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        
        # Reload scheduler to apply changes immediately
        scheduler.reload_config()
        
        bot.reply_to(message, f"⚪ Job `{job_name}` disabled", parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error disabling job: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

# ========================================
# Results and Logs Commands
# ========================================

@bot.message_handler(commands=['results'])
def show_results(message):
    """
    Show the latest trading results to the authorized Telegram user.
    
    Reads the most recent result file from the results directory and sends a formatted summary plus file metadata to the chat. If no result file is found, sends a helpful message explaining possible reasons and guidance. Requires the sender to be authorized; replies are posted using the bot.
    
    Parameters:
        message: The incoming Telegram message object that triggered this command; used to determine the chat and to reply.
    """
    if not is_authorized(message):
        return
    
    try:
        latest_file = get_latest_result_file()
        
        if not latest_file:
            # Check if directory exists
            if not os.path.exists(RESULTS_DIR):
                bot.reply_to(message, 
                    "📊 *No Trading Results*\n\n"
                    "No results found yet.\n\n"
                    "Results will appear here after you run:\n"
                    "/trade - Run trading manually\n\n"
                    "Or after scheduled trading executes.",
                    parse_mode='Markdown'
                )
            else:
                bot.reply_to(message,
                    "📊 *No Trading Results*\n\n"
                    f"Results directory is empty.\n\n"
                    "This can mean:\n"
                    "• Trading hasn't run yet today\n"
                    "• No orders were placed\n"
                    "• Market was closed\n\n"
                    "Try:\n"
                    "/status - Check system\n"
                    "/logs - View recent activity",
                    parse_mode='Markdown'
                )
            return
        
        # Format and send results
        result_msg = format_order_results(latest_file)
        bot.reply_to(message, result_msg, parse_mode='Markdown')
        
        # Show file info
        file_path = Path(latest_file)
        file_time = datetime.fromtimestamp(file_path.stat().st_mtime)
        bot.send_message(
            message.chat.id,
            f"📁 File: `{file_path.name}`\n"
            f"🕐 Modified: {file_time.strftime('%Y-%m-%d %H:%M:%S')}",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error showing results: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['logs'])
def show_logs(message):
    """
    Display recent lines from the application log to the authorized Telegram user.
    
    Parses an optional integer argument from the triggering message (e.g., "/logs 100") to specify how many lines to show. Defaults to 50 lines and is clamped to the range 10–200. If the rendered output exceeds Telegram's message size limits, the output is split into multiple messages. After sending log contents, the command also sends the log file's size and last modification time when the log file exists. Requires the sender to be authorized; replies with an error message on invalid input or on internal failure.
    
    Parameters:
        message: Telegram message object that invoked the command (message.text may include an optional line count).
    """
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        lines = 50  # Default
        
        if len(parts) > 1:
            try:
                lines = int(parts[1])
                lines = max(10, min(lines, 200))  # Clamp between 10-200
            except ValueError:
                bot.reply_to(message, "❌ Invalid number. Using default (50 lines)")
                lines = 50
        
        log_text = get_log_tail(lines)
        
        # Send in chunks if needed (Telegram has 4096 char limit)
        if len(log_text) <= 4096:
            bot.reply_to(message, log_text, parse_mode='Markdown')
        else:
            # Split into chunks
            chunks = [log_text[i:i+4000] for i in range(0, len(log_text), 4000)]
            for i, chunk in enumerate(chunks, 1):
                if i == 1:
                    bot.reply_to(message, f"Part {i}/{len(chunks)}:\n{chunk}", parse_mode='Markdown')
                else:
                    bot.send_message(message.chat.id, f"Part {i}/{len(chunks)}:\n{chunk}", parse_mode='Markdown')
        
        # Show file info
        if os.path.exists(LOG_FILE):
            file_size = os.path.getsize(LOG_FILE)
            file_time = datetime.fromtimestamp(os.path.getmtime(LOG_FILE))
            
            bot.send_message(
                message.chat.id,
                f"📁 *Log File Info:*\n"
                f"Size: {file_size:,} bytes\n"
                f"Modified: {file_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Use `/logs <number>` to view different amount (10-200)",
                parse_mode='Markdown'
            )
        
    except Exception as e:
        logger.error(f"Error showing logs: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['stop'])
def stop_trading(message):
    """
    Stop all running trading and cache processes and report the result to the user.
    
    Attempts to terminate any tracked subprocesses, then force-kills remaining platform-specific
    trading processes (Locust and cache_warmup). Sends a Telegram reply to the triggering message
    with a summary of actions taken.
    
    Parameters:
        message: The Telegram message object that triggered this command; used to reply to the user.
    """
    if not is_authorized(message):
        return
    
    try:
        bot.reply_to(message, "🛑 Stopping all trading processes...")
        
        killed_count = 0
        messages = []
        
        # First, kill tracked processes
        with process_lock:
            for proc_name, proc in list(running_processes.items()):
                try:
                    if proc.poll() is None:  # Process is still running
                        proc.terminate()
                        proc.wait(timeout=3)
                        messages.append(f"✅ Stopped {proc_name}")
                        killed_count += 1
                except Exception as e:
                    logger.error(f"Error stopping tracked process {proc_name}: {e}")
                finally:
                    running_processes.pop(proc_name, None)
        
        # Then, force kill any remaining locust processes
        import platform
        if platform.system() == 'Windows':
            # Kill all locust.exe
            try:
                locust_result = subprocess.run(
                    ['taskkill', '/F', '/IM', 'locust.exe', '/T'],
                    capture_output=True,
                    text=True
                )
                if 'SUCCESS' in locust_result.stdout or 'terminated' in locust_result.stdout.lower():
                    messages.append("✅ Killed locust.exe processes")
                    killed_count += 1
            except Exception as e:
                logger.error(f"Error killing locust: {e}")
            
            # Kill Python processes running trading scripts
            try:
                ps_command = (
                    'Get-Process python -ErrorAction SilentlyContinue | '
                    'Where-Object { '
                    '  $cmd = $_.CommandLine; '
                    '  $cmd -like "*cache_warmup*" -or '
                    '  $cmd -like "*locustfile*" -or '
                    '  $cmd -like "*locust*" '
                    '} | '
                    'ForEach-Object { '
                    '  Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue; '
                    '  $_.ProcessName '
                    '}'
                )
                python_result = subprocess.run(
                    ['powershell', '-Command', ps_command],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if python_result.stdout.strip():
                    messages.append("✅ Killed Python trading processes")
                    killed_count += 1
            except Exception as e:
                logger.error(f"Error killing Python processes: {e}")
        else:
            # Linux/Mac: Use pkill
            try:
                subprocess.run(['pkill', '-9', '-f', 'locust'], capture_output=True)
                subprocess.run(['pkill', '-9', '-f', 'cache_warmup'], capture_output=True)
                subprocess.run(['pkill', '-9', '-f', 'locustfile'], capture_output=True)
                messages.append("✅ Killed all trading processes")
                killed_count += 1
            except Exception as e:
                logger.error(f"Error killing processes: {e}")
        
        # Send result
        if killed_count == 0:
            bot.reply_to(message, "ℹ️ No running trading processes found")
        else:
            response = "🛑 *Stopped All Processes:*\n\n" + "\n".join(messages)
            bot.reply_to(message, response, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error stopping processes: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

# ========================================
# Catch-all handler for unknown commands
# THIS MUST BE THE LAST HANDLER!
# ========================================

@bot.message_handler(func=lambda m: True)
def handle_unknown(message):
    """
    Reply to the sender indicating the command is unrecognized and suggest using /help.
    
    Parameters:
        message: Telegram message object representing the incoming unknown command; ignored if the sender is not authorized.
    """
    if not is_authorized(message):
        return
    bot.reply_to(message, "❌ Unknown command. Send /help for available commands.")

def main():
    """
    Start the Telegram bot, run the background scheduler, and maintain an automatic restart loop on errors.
    
    Validates required environment variables and logs startup information, starts the scheduler in the background, then enters a persistent polling loop that auto-restarts the bot after unexpected exceptions. On KeyboardInterrupt the scheduler is stopped and the function exits gracefully.
    """
    # Validate environment variables before starting
    validate_environment()
    
    logger.info("Starting Simple Trading Config Bot...")
    logger.info(f"Config file: {CONFIG_FILE}")
    if USER_ID:
        logger.info(f"Authorized user: {USER_ID}")
    else:
        logger.warning("No USER_ID set - bot accessible to all users!")
    
    # Start scheduler in background
    scheduler.start()
    logger.info("📅 Background scheduler started")
    
    restart_count = 0
    
    # Infinite restart loop - bot will NEVER give up
    while True:
        try:
            if restart_count > 0:
                logger.info(f"Bot restart #{restart_count}")
            
            logger.info("Bot started. Polling for messages...")
            
            # Set longer timeouts and enable auto-restart
            bot.infinity_polling(
                timeout=90,           # Request timeout
                long_polling_timeout=60,  # Long polling timeout  
                skip_pending=True,    # Skip old messages on restart
                none_stop=True        # Never stop on errors
            )
            
        except KeyboardInterrupt:
            logger.info("Bot stopped by user (Ctrl+C)")
            print("\n\n✅ Bot stopped gracefully")
            scheduler.stop()
            break
            
        except Exception as e:
            restart_count += 1
            logger.error(f"Bot error (restart #{restart_count}): {e}")
            logger.info("Restarting in 5 seconds...")
            
            # Show error but keep going
            print(f"\n⚠️  Error: {e}")
            print(f"🔄 Auto-restarting in 5 seconds... (restart #{restart_count})")
            
            import time
            time.sleep(5)

if __name__ == '__main__':
    main()