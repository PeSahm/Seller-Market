#!/usr/bin/env python3
"""
Simple Telegram Bot for Trading Configuration
Directly updates config.ini file
"""

import telebot
import configparser
import os
import logging
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(env_path):
    load_dotenv(env_path)

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

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not set")

bot = telebot.TeleBot(BOT_TOKEN)

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
    """Save config.ini"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        config.write(f)
    logger.info("Configuration saved")

def get_active_section(config):
    """Get the first active (non-commented) section"""
    for section in config.sections():
        if not section.startswith('#') and not section.startswith(';'):
            return section
    return None

def set_active_section(config_file, section_name):
    """Make a section active by uncommenting it and commenting others"""
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
    """List all available configurations"""
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
    """Switch to a different configuration"""
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

*Example:*
/list
/add Account2
/use Account2
/broker bbi
/symbol IRO1FOLD0001
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

@bot.message_handler(func=lambda m: True)
def handle_unknown(message):
    """Handle unknown commands"""
    if not is_authorized(message):
        return
    bot.reply_to(message, "❌ Unknown command. Send /help for available commands.")

def main():
    """Start the bot"""
    logger.info("Starting Simple Trading Config Bot...")
    logger.info(f"Config file: {CONFIG_FILE}")
    if USER_ID:
        logger.info(f"Authorized user: {USER_ID}")
    else:
        logger.warning("No USER_ID set - bot accessible to all users!")
    
    try:
        logger.info("Bot started. Polling for messages...")
        bot.infinity_polling()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot error: {e}")

if __name__ == '__main__':
    main()
