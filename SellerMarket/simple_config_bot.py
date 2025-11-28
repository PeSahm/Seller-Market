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
SELECTED_SECTION_FILE = os.path.join(os.path.dirname(__file__), '.selected_section')

# Validate environment variables only when running the bot (not when importing for tests)
def validate_environment():
    """Validate required environment variables"""
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
    """Get Windows system proxy settings from registry"""
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
    """Check if user is authorized"""
    if USER_ID and str(message.from_user.id) != str(USER_ID):
        bot.reply_to(message, "❌ Unauthorized")
        return False
    return True

def read_config():
    """Read current config.ini"""
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE, encoding='utf-8')
    return config

def save_config(config):
    """
    Save config.ini - simple write since we no longer use comments for section management.
    """
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        config.write(f)
    logger.info("Configuration saved")

def get_all_result_files() -> list:
    """Get all order result files sorted by modification time (newest first)"""
    try:
        if not os.path.exists(RESULTS_DIR):
            return []
        
        files = [f for f in Path(RESULTS_DIR).glob('*.json')]
        if not files:
            return []
        
        # Sort by modification time, most recent first
        sorted_files = sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)
        return [str(f) for f in sorted_files]
    except Exception as e:
        logger.error(f"Error finding result files: {e}")
        return []

def format_complete_order_results(result_files: list, max_files: int = 3) -> str:
    """Format complete order results for all recent files"""
    if not result_files:
        return "📊 *No Trading Results Found*"
    
    all_messages = []
    
    # Process up to max_files most recent files
    for i, result_file in enumerate(result_files[:max_files], 1):
        try:
            with open(result_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            username = data.get('username', 'Unknown')
            broker = data.get('broker_code', 'Unknown')
            timestamp = data.get('timestamp', '')
            orders = data.get('orders', [])
            
            file_path = Path(result_file)
            file_time = datetime.fromtimestamp(file_path.stat().st_mtime)
            
            # File header
            msg = f"📊 *Results #{i}* - `{file_path.name}`\n"
            msg += f"👤 Account: `{username}@{broker}`\n"
            msg += f"🕐 File Time: {file_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            msg += f"🕑 Order Time: {datetime.fromisoformat(timestamp).strftime('%H:%M:%S') if timestamp else 'N/A'}\n\n"
            
            if not orders:
                msg += "⚠️ No orders in this file\n\n"
                all_messages.append(msg)
                continue
            
            # Calculate summary
            total_volume = sum(o.get('volume', 0) for o in orders)
            total_executed = sum(o.get('executed_volume', 0) for o in orders)
            total_amount = sum(o.get('net_amount', 0) for o in orders)
            
            msg += f"📈 *Summary:*\n"
            msg += f"  Orders: {len(orders)}\n"
            msg += f"  Volume: {total_volume:,} shares\n"
            msg += f"  Executed: {total_executed:,} ({total_executed/total_volume*100:.1f}%)\n" if total_volume > 0 else "  Executed: 0\n"
            msg += f"  Amount: {total_amount:,.0f} Rials\n\n"
            
            # Show all orders with complete details
            msg += f"📋 *Order Details:*\n"
            for j, order in enumerate(orders, 1):
                symbol = order.get('symbol', 'N/A')
                tracking_number = order.get('tracking_number', 'N/A')
                state_desc = order.get('state_desc', 'Unknown')
                created_shamsi = order.get('created_shamsi', 'N/A')
                side = "BUY" if order.get('side') == 1 else "SELL"
                volume = order.get('volume', 0)
                price = order.get('price', 0)
                executed = order.get('executed_volume', 0)
                
                msg += f"{j}. *{symbol}* ({side})\n"
                msg += f"   📊 Tracking: `{tracking_number}`\n"
                msg += f"   📅 Created: {created_shamsi}\n"
                msg += f"   📈 Volume: {volume:,} | Price: {price:,}\n"
                msg += f"   ✅ Executed: {executed:,}/{volume:,}\n"
                msg += f"   📋 Status: {state_desc}\n\n"
            
            all_messages.append(msg)
            
        except Exception as e:
            logger.error(f"Error formatting file {result_file}: {e}")
            all_messages.append(f"❌ Error reading file: {Path(result_file).name}\n\n")
    
    # Add summary if multiple files
    if len(result_files) > max_files:
        all_messages.append(f"📁 *{len(result_files) - max_files} more result files available*\n\n")
    
    return "".join(all_messages)

def get_log_tail(lines: int = 50) -> str:
    """Get last N lines from trading_bot.log"""
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

def get_locust_config():
    """
    Load Locust configuration from locust_config.json.
    Note: The 'host' parameter is required by Locust CLI but ignored at runtime.
    Actual broker URLs are dynamically constructed in broker_enum.py.
    """
    locust_config_file = os.path.join(os.path.dirname(__file__), 'locust_config.json')
    try:
        with open(locust_config_file, 'r') as f:
            config = json.load(f)
        return config.get('locust', {})
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Could not load locust config: {e}. Using defaults.")
        return {
            'users': 10,
            'spawn_rate': 10,
            'run_time': '30s',
            'host': 'https://abc.com',
            'html_report': 'report.html'
        }

def get_selected_section():
    """
    Get the currently selected section for editing.
    Returns the saved selection or the first available section.
    """
    # Try to read from file
    if os.path.exists(SELECTED_SECTION_FILE):
        try:
            with open(SELECTED_SECTION_FILE, 'r', encoding='utf-8') as f:
                selected = f.read().strip()
                if selected:
                    # Verify it still exists in config
                    config = read_config()
                    if selected in config.sections():
                        return selected
        except Exception as e:
            logger.warning(f"Could not read selected section file: {e}")
    
    # Fall back to first section
    config = read_config()
    sections = config.sections()
    return sections[0] if sections else None

def set_selected_section(section_name):
    """
    Set the currently selected section for editing.
    This does NOT comment out other sections - all sections remain active for trading.
    """
    try:
        with open(SELECTED_SECTION_FILE, 'w', encoding='utf-8') as f:
            f.write(section_name)
        logger.info(f"Selected section for editing: {section_name}")
        return True
    except Exception as e:
        logger.error(f"Could not save selected section: {e}")
        return False

# Keep old function names for backward compatibility
def get_active_section(config=None):
    """Get the currently selected section for editing (backward compatible)."""
    return get_selected_section()

def set_active_section(config_file, section_name):
    """Set the selected section (backward compatible - no longer comments out sections)."""
    return set_selected_section(section_name)

@bot.message_handler(commands=['list'])
def list_configs(message):
    """List all available configurations"""
    if not is_authorized(message):
        return
    
    try:
        config = read_config()
        sections = config.sections()
        selected_section = get_selected_section()
        
        if not sections:
            bot.reply_to(message, "📝 No configurations found\n\nUse /add <name> to create one")
            return
        
        section_list = []
        for section_name in sections:
            is_selected = (section_name == selected_section)
            status = "✏️ EDITING" if is_selected else "✅"
            section_list.append(f"{status} `{section_name}`")
        
        response = f"📋 *All Configs ({len(sections)} accounts):*\n\n"
        response += "\n".join(section_list)
        response += "\n\n✏️ = Currently editing\n✅ = Active for trading"
        response += "\n\nUse `/use <name>` to select for editing"
        
        bot.reply_to(message, response, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error listing configs: {e}")
        bot.reply_to(message, f"❌ Error listing configurations: {e}")

@bot.message_handler(commands=['use'])
def use_config(message):
    """Select a configuration for editing (does NOT disable other configs)"""
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
        all_sections = config.sections()
        
        if target_section not in all_sections:
            available = ', '.join([f'`{s}`' for s in all_sections[:5]])
            if len(all_sections) > 5:
                available += f' ... ({len(all_sections)} total)'
            bot.reply_to(message, f"❌ Config `{target_section}` not found\n\nAvailable: {available}", parse_mode='Markdown')
            return
        
        set_selected_section(target_section)
        bot.reply_to(message, 
            f"✏️ Now editing: `{target_section}`\n\n"
            f"Use /broker, /symbol, /side, /user, /pass to update this config.\n"
            f"All {len(all_sections)} configs remain active for trading.", 
            parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error switching config: {e}")
        bot.reply_to(message, "❌ Error switching configuration")

@bot.message_handler(commands=['add'])
def add_config(message):
    """Add a new configuration"""
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
    """Remove a configuration"""
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
    """Show help message"""
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
    """Show current configuration"""
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
    """Set broker"""
    if not is_authorized(message):
        return
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /broker <code>\nExample: /broker gs")
            return
        
        broker = parts[1].lower()
        valid_brokers = ['gs', 'bbi', 'shahr', 'karamad', 'tejarat', 'ebb']
        
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
            'ebb': 'Ebb'
        }
        
        bot.reply_to(message, f"✅ Broker set to: *{broker_names.get(broker, broker)}*", parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error setting broker: {e}")
        bot.reply_to(message, "❌ Error updating broker")

@bot.message_handler(commands=['symbol'])
def set_symbol(message):
    """Set stock symbol"""
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
    """Set trade side"""
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
    """Set username"""
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
    """Set password"""
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
    """Run cache warmup manually"""
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
    """Run trading bot manually"""
    if not is_authorized(message):
        return
    
    try:
        locust_config = get_locust_config()
        users = locust_config.get('users', 10)
        spawn_rate = locust_config.get('spawn_rate', 10)
        run_time = locust_config.get('run_time', '30s')
        host = locust_config.get('host', 'https://abc.com')
        
        bot.reply_to(message, f"🚀 Starting trading bot...\nUsers: {users}, Spawn rate: {spawn_rate}, Run time: {run_time}")
        
        result = subprocess.run(
            ['locust', '-f', 'locustfile_new.py', '--headless', '--users', str(users), '--spawn-rate', str(spawn_rate), '--run-time', run_time, '--host', host],
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
    """Show system status"""
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
    """Show scheduled jobs"""
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
    """Set cache warmup time"""
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
    """Set trading time"""
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
            locust_config = get_locust_config()
            users = locust_config.get('users', 10)
            spawn_rate = locust_config.get('spawn_rate', 10)
            run_time = locust_config.get('run_time', '30s')
            host = locust_config.get('host', 'https://abc.com')
            
            config['jobs'].append({
                "name": "run_trading",
                "time": time_str,
                "command": f"locust -f locustfile_new.py --headless --users {users} --spawn-rate {spawn_rate} --run-time {run_time} --host {host}",
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
    """Enable a scheduled job"""
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
    """Disable a scheduled job"""
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
    """Show latest trading results"""
    if not is_authorized(message):
        return
    
    try:
        result_files = get_all_result_files()
        
        if not result_files:
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
        
        # Format and send complete results for all recent files
        result_msg = format_complete_order_results(result_files, max_files=3)
        
        # Send in chunks if message is too long (Telegram limit is 4096 chars)
        if len(result_msg) <= 4000:
            bot.reply_to(message, result_msg, parse_mode='Markdown')
        else:
            # Split into chunks
            chunks = []
            current_chunk = ""
            
            for line in result_msg.split('\n'):
                if len(current_chunk + line + '\n') > 3800:
                    chunks.append(current_chunk)
                    current_chunk = line + '\n'
                else:
                    current_chunk += line + '\n'
            
            if current_chunk:
                chunks.append(current_chunk)
            
            # Send chunks
            for i, chunk in enumerate(chunks, 1):
                if i == 1:
                    bot.reply_to(message, f"📊 *Trading Results* (Part {i}/{len(chunks)})\n\n{chunk}", parse_mode='Markdown')
                else:
                    bot.send_message(message.chat.id, f"📊 *Results Continued* (Part {i}/{len(chunks)})\n\n{chunk}", parse_mode='Markdown')
        
        # Show summary info
        total_files = len(result_files)
        bot.send_message(
            message.chat.id,
            f"📁 *Summary:*\n"
            f"Total result files: {total_files}\n"
            f"Showing latest: {min(3, total_files)}\n\n"
            f"� Directory: `{RESULTS_DIR}`\n"
            f"📋 Use /logs to see execution details",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error showing results: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['logs'])
def show_logs(message):
    """Show recent log entries"""
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
    """Stop any running trading/cache processes"""
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
    """Handle unknown commands"""
    if not is_authorized(message):
        return
    bot.reply_to(message, "❌ Unknown command. Send /help for available commands.")

def main():
    """Start the bot with unlimited auto-restart on errors"""
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
