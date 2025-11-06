#!/usr/bin/env python3
"""
Telegram Configuration Bot
Remote management interface for trading bot configurations and order notifications
"""

import telebot
from telebot import types
import requests
import json
import logging
from datetime import datetime
import os

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class TradingConfigBot:
    def __init__(self, token, api_url):
        self.bot = telebot.TeleBot(token)
        self.api_url = api_url
        self.user_states = {}  # Track user conversation states
        self.setup_handlers()
        logger.info("TradingConfigBot initialized")

    def setup_handlers(self):
        """Set up all message and command handlers"""

        @self.bot.message_handler(commands=['start'])
        def send_welcome(message):
            """Welcome message and help"""
            user_id = message.from_user.id
            welcome_text = """
🤖 *Trading Bot Configuration Manager*

Welcome! I help you manage your trading bot configurations remotely.

*Available Commands:*
/list_configs - Show all your configurations
/select_config <name> - Switch active configuration
/set_broker <code> - Set broker (gs/bbi/shahr/karamad/tejarat/shams)
/set_credentials - Securely set username/password
/set_symbol <ISIN> - Set stock symbol
/set_side <1|2> - Set buy (1) or sell (2)
/get_config - Show current active configuration
/get_results - Show recent order results
/status - Show bot and system status
/help - Show this help message

*Quick Start:*
1. Use /list_configs to see your configurations
2. Use /select_config to choose one
3. Use /set_* commands to modify settings
4. Use /get_config to verify changes

Your configurations are stored securely and only accessible by you.
            """
            self.bot.reply_to(message, welcome_text, parse_mode='Markdown')

        @self.bot.message_handler(commands=['help'])
        def send_help(message):
            """Show help message"""
            help_text = """
📚 *Available Commands*

*Configuration Management:*
/list_configs - List all your configurations
/select_config <name> - Switch active configuration
/get_config - Show current active configuration

*Settings Commands:*
/set_broker <code> - Set broker (gs/bbi/shahr/karamad/tejarat/shams)
/set_symbol <ISIN> - Set stock symbol (e.g., IRO1MHRN0001)
/set_side <1|2> - Set trade side (1=Buy, 2=Sell)
/set_credentials - Securely set username/password

*Monitoring Commands:*
/get_results - Show recent order results
/status - Show system status

*Other Commands:*
/start - Show welcome message
/help - Show this help

*Examples:*
• `/set_broker gs` - Switch to Ganjine broker
• `/set_symbol IRO1MHRN0001` - Trade فولاد stock
• `/select_config Mostafa_gs_` - Use your main config
            """
            self.bot.reply_to(message, help_text, parse_mode='Markdown')

        @self.bot.message_handler(commands=['list_configs'])
        def list_configs(message):
            """List all user configurations"""
            user_id = message.from_user.id
            try:
                response = requests.get(f"{self.api_url}/config/{user_id}/list")
                if response.status_code == 200:
                    data = response.json()
                    configs = data.get('configs', [])
                    active_config = data.get('active_config')

                    if not configs:
                        self.bot.reply_to(message, "📝 No configurations found. Use /set_broker to create your first configuration.")
                        return

                    config_list = "📋 *Your Configurations:*\n\n"
                    for config in configs:
                        status = " ✅ ACTIVE" if config == active_config else ""
                        config_list += f"• `{config}`{status}\n"

                    config_list += f"\n*Active:* `{active_config or 'None'}`"
                    config_list += "\n\nUse `/select_config <name>` to switch active configuration."

                    self.bot.reply_to(message, config_list, parse_mode='Markdown')
                else:
                    self.bot.reply_to(message, "❌ Error retrieving configurations.")
            except Exception as e:
                logger.error(f"Error listing configs: {e}")
                self.bot.reply_to(message, "❌ Error connecting to configuration server.")

        @self.bot.message_handler(commands=['select_config'])
        def select_config(message):
            """Select active configuration"""
            user_id = message.from_user.id
            try:
                # Extract config name from command
                parts = message.text.split()
                if len(parts) < 2:
                    self.bot.reply_to(message, "❌ Please specify configuration name.\n\nUsage: `/select_config <config_name>`\n\nUse /list_configs to see available configurations.", parse_mode='Markdown')
                    return

                config_name = parts[1]

                # First check if config exists
                response = requests.get(f"{self.api_url}/config/{user_id}/list")
                if response.status_code == 200:
                    data = response.json()
                    configs = data.get('configs', [])
                    if config_name not in configs:
                        available = ", ".join(configs) if configs else "none"
                        self.bot.reply_to(message, f"❌ Configuration '{config_name}' not found.\n\nAvailable: {available}")
                        return

                # Set active config
                response = requests.post(f"{self.api_url}/config/{user_id}/active/{config_name}")
                if response.status_code == 200:
                    self.bot.reply_to(message, f"✅ Active configuration set to: `{config_name}`", parse_mode='Markdown')
                else:
                    self.bot.reply_to(message, "❌ Error setting active configuration.")

            except Exception as e:
                logger.error(f"Error selecting config: {e}")
                self.bot.reply_to(message, "❌ Error selecting configuration.")

        @self.bot.message_handler(commands=['get_config'])
        def get_config(message):
            """Show current active configuration"""
            user_id = message.from_user.id
            try:
                response = requests.get(f"{self.api_url}/config/{user_id}")
                if response.status_code == 200:
                    config = response.json()

                    config_text = "⚙️ *Current Configuration:*\n\n"
                    config_text += f"🏛️ *Broker:* `{config.get('broker', 'Not set')}`\n"
                    config_text += f"📈 *Symbol:* `{config.get('isin', 'Not set')}`\n"
                    config_text += f"📊 *Side:* `{'BUY' if config.get('side') == 1 else 'SELL'}`\n"
                    config_text += f"👤 *Username:* `{config.get('username', 'Not set')}`\n"

                    if config.get('updated_at'):
                        updated = datetime.fromisoformat(config['updated_at'].replace('Z', '+00:00'))
                        config_text += f"🕒 *Last Updated:* {updated.strftime('%Y-%m-%d %H:%M:%S')}"

                    self.bot.reply_to(message, config_text, parse_mode='Markdown')
                else:
                    self.bot.reply_to(message, "❌ Error retrieving configuration.")
            except Exception as e:
                logger.error(f"Error getting config: {e}")
                self.bot.reply_to(message, "❌ Error retrieving configuration.")

        @self.bot.message_handler(commands=['set_broker'])
        def set_broker(message):
            """Set broker for active configuration"""
            user_id = message.from_user.id
            try:
                parts = message.text.split()
                if len(parts) < 2:
                    markup = types.ReplyKeyboardMarkup(row_width=2, one_time_keyboard=True)
                    brokers = ['gs', 'bbi', 'shahr', 'karamad', 'tejarat', 'shams']
                    markup.add(*brokers)
                    self.bot.reply_to(message, "🏛️ Select your broker:", reply_markup=markup)
                    self.user_states[user_id] = {'waiting_for': 'broker'}
                    return

                broker = parts[1].lower()
                valid_brokers = ['gs', 'bbi', 'shahr', 'karamad', 'tejarat', 'shams']

                if broker not in valid_brokers:
                    self.bot.reply_to(message, f"❌ Invalid broker. Valid options: {', '.join(valid_brokers)}")
                    return

                # Get active config name first
                list_response = requests.get(f"{self.api_url}/config/{user_id}/list")
                active_config = 'default'
                if list_response.status_code == 200:
                    data = list_response.json()
                    active_config = data.get('active_config', 'default')

                # Update broker
                update_data = {'broker': broker}
                response = requests.post(f"{self.api_url}/config/{user_id}/{active_config}", json=update_data)

                if response.status_code == 200:
                    broker_names = {
                        'gs': 'Ganjine (Ghadir Shahr)',
                        'bbi': 'Bourse Bazar Iran',
                        'shahr': 'Shahr',
                        'karamad': 'Karamad',
                        'tejarat': 'Tejarat',
                        'shams': 'Shams'
                    }
                    self.bot.reply_to(message, f"✅ Broker set to: *{broker_names.get(broker, broker)}*", parse_mode='Markdown')
                else:
                    self.bot.reply_to(message, "❌ Error updating broker.")

            except Exception as e:
                logger.error(f"Error setting broker: {e}")
                self.bot.reply_to(message, "❌ Error setting broker.")

        @self.bot.message_handler(commands=['set_symbol'])
        def set_symbol(message):
            """Set stock symbol for active configuration"""
            user_id = message.from_user.id
            try:
                parts = message.text.split()
                if len(parts) < 2:
                    self.bot.reply_to(message, "❌ Please specify stock symbol.\n\nUsage: `/set_symbol <ISIN>`\n\nExample: `/set_symbol IRO1MHRN0001`", parse_mode='Markdown')
                    return

                symbol = parts[1].upper()

                # Get active config name
                list_response = requests.get(f"{self.api_url}/config/{user_id}/list")
                active_config = 'default'
                if list_response.status_code == 200:
                    data = list_response.json()
                    active_config = data.get('active_config', 'default')

                # Update symbol
                update_data = {'isin': symbol}
                response = requests.post(f"{self.api_url}/config/{user_id}/{active_config}", json=update_data)

                if response.status_code == 200:
                    self.bot.reply_to(message, f"✅ Stock symbol set to: `{symbol}`", parse_mode='Markdown')
                else:
                    self.bot.reply_to(message, "❌ Error updating stock symbol.")

            except Exception as e:
                logger.error(f"Error setting symbol: {e}")
                self.bot.reply_to(message, "❌ Error setting stock symbol.")

        @self.bot.message_handler(commands=['set_side'])
        def set_side(message):
            """Set trade side for active configuration"""
            user_id = message.from_user.id
            try:
                parts = message.text.split()
                if len(parts) < 2:
                    markup = types.ReplyKeyboardMarkup(row_width=2, one_time_keyboard=True)
                    markup.add('1 (Buy)', '2 (Sell)')
                    self.bot.reply_to(message, "📊 Select trade side:", reply_markup=markup)
                    self.user_states[user_id] = {'waiting_for': 'side'}
                    return

                side_str = parts[1]
                if side_str not in ['1', '2']:
                    self.bot.reply_to(message, "❌ Invalid side. Use 1 for Buy or 2 for Sell.")
                    return

                side = int(side_str)
                side_name = 'BUY' if side == 1 else 'SELL'

                # Get active config name
                list_response = requests.get(f"{self.api_url}/config/{user_id}/list")
                active_config = 'default'
                if list_response.status_code == 200:
                    data = list_response.json()
                    active_config = data.get('active_config', 'default')

                # Update side
                update_data = {'side': side}
                response = requests.post(f"{self.api_url}/config/{user_id}/{active_config}", json=update_data)

                if response.status_code == 200:
                    self.bot.reply_to(message, f"✅ Trade side set to: *{side_name}*", parse_mode='Markdown')
                else:
                    self.bot.reply_to(message, "❌ Error updating trade side.")

            except Exception as e:
                logger.error(f"Error setting side: {e}")
                self.bot.reply_to(message, "❌ Error setting trade side.")

        @self.bot.message_handler(commands=['set_credentials'])
        def set_credentials(message):
            """Securely set username and password"""
            user_id = message.from_user.id
            self.bot.reply_to(message, "🔐 *Credential Setup*\n\nPlease send your username:", parse_mode='Markdown')
            self.user_states[user_id] = {'waiting_for': 'username'}

        @self.bot.message_handler(commands=['get_results'])
        def get_results(message):
            """Show recent order results"""
            user_id = message.from_user.id
            try:
                response = requests.get(f"{self.api_url}/results/{user_id}?limit=5")
                if response.status_code == 200:
                    results = response.json()

                    if not results:
                        self.bot.reply_to(message, "📊 No order results found.")
                        return

                    results_text = "📊 *Recent Order Results:*\n\n"
                    for i, result_entry in enumerate(results[-5:], 1):  # Show last 5
                        result = result_entry.get('result', {})
                        timestamp = datetime.fromisoformat(result_entry['timestamp'].replace('Z', '+00:00'))

                        status_emoji = "✅" if result.get('status') == 'SUCCESS' else "❌"
                        side = "BUY" if result.get('side') == 1 else "SELL"

                        results_text += f"{status_emoji} *Order {i}:*\n"
                        results_text += f"  📈 Symbol: `{result.get('symbol', 'N/A')}`\n"
                        results_text += f"  📊 Side: {side}\n"
                        results_text += f"  📦 Volume: {result.get('volume', 'N/A'):,} shares\n"
                        results_text += f"  💰 Price: {result.get('price', 'N/A'):,} Rials\n"
                        results_text += f"  🕒 Time: {timestamp.strftime('%H:%M:%S')}\n\n"

                    self.bot.reply_to(message, results_text, parse_mode='Markdown')
                else:
                    self.bot.reply_to(message, "❌ Error retrieving order results.")
            except Exception as e:
                logger.error(f"Error getting results: {e}")
                self.bot.reply_to(message, "❌ Error retrieving order results.")

        @self.bot.message_handler(commands=['status'])
        def get_status(message):
            """Show system status"""
            user_id = message.from_user.id
            try:
                # Check API health
                health_response = requests.get(f"{self.api_url}/health")
                api_status = "✅ Online" if health_response.status_code == 200 else "❌ Offline"

                # Get user config count
                config_response = requests.get(f"{self.api_url}/config/{user_id}/list")
                config_count = 0
                if config_response.status_code == 200:
                    data = config_response.json()
                    config_count = len(data.get('configs', []))

                # Get recent results count
                results_response = requests.get(f"{self.api_url}/results/{user_id}?limit=100")
                results_count = 0
                if results_response.status_code == 200:
                    results_count = len(results_response.json())

                status_text = f"""
🤖 *System Status*

🌐 *API Server:* {api_status}
👤 *Your Configs:* {config_count}
📊 *Your Results:* {results_count}
🕒 *Server Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
                """

                self.bot.reply_to(message, status_text.strip(), parse_mode='Markdown')

            except Exception as e:
                logger.error(f"Error getting status: {e}")
                self.bot.reply_to(message, "❌ Error retrieving system status.")

        @self.bot.message_handler(func=lambda message: True)
        def handle_text(message):
            """Handle text messages (for credential setup)"""
            user_id = message.from_user.id
            user_state = self.user_states.get(user_id, {})

            if user_state.get('waiting_for') == 'username':
                username = message.text.strip()
                self.user_states[user_id] = {'waiting_for': 'password', 'username': username}
                self.bot.reply_to(message, "🔑 Now send your password:")

            elif user_state.get('waiting_for') == 'password':
                password = message.text.strip()
                username = user_state.get('username')

                if not username or not password:
                    self.bot.reply_to(message, "❌ Error: Missing username or password.")
                    del self.user_states[user_id]
                    return

                # Get active config name
                try:
                    list_response = requests.get(f"{self.api_url}/config/{user_id}/list")
                    active_config = 'default'
                    if list_response.status_code == 200:
                        data = list_response.json()
                        active_config = data.get('active_config', 'default')

                    # Update credentials
                    update_data = {'username': username, 'password': password}
                    response = requests.post(f"{self.api_url}/config/{user_id}/{active_config}", json=update_data)

                    if response.status_code == 200:
                        self.bot.reply_to(message, "✅ Credentials updated successfully!")
                    else:
                        self.bot.reply_to(message, "❌ Error updating credentials.")

                except Exception as e:
                    logger.error(f"Error updating credentials: {e}")
                    self.bot.reply_to(message, "❌ Error updating credentials.")

                # Clear user state
                del self.user_states[user_id]

            elif user_state.get('waiting_for') == 'broker':
                broker = message.text.lower()
                valid_brokers = ['gs', 'bbi', 'shahr', 'karamad', 'tejarat', 'shams']

                if broker in valid_brokers:
                    # Get active config name
                    try:
                        list_response = requests.get(f"{self.api_url}/config/{user_id}/list")
                        active_config = 'default'
                        if list_response.status_code == 200:
                            data = list_response.json()
                            active_config = data.get('active_config', 'default')

                        # Update broker
                        update_data = {'broker': broker}
                        response = requests.post(f"{self.api_url}/config/{user_id}/{active_config}", json=update_data)

                        if response.status_code == 200:
                            self.bot.reply_to(message, f"✅ Broker set to: *{broker}*", parse_mode='Markdown')
                        else:
                            self.bot.reply_to(message, "❌ Error updating broker.")

                    except Exception as e:
                        logger.error(f"Error setting broker: {e}")
                        self.bot.reply_to(message, "❌ Error setting broker.")
                else:
                    self.bot.reply_to(message, f"❌ Invalid broker. Valid options: {', '.join(valid_brokers)}")

                # Clear keyboard and state
                markup = types.ReplyKeyboardRemove()
                self.bot.send_message(message.chat.id, "Keyboard removed.", reply_markup=markup)
                del self.user_states[user_id]

            elif user_state.get('waiting_for') == 'side':
                if 'Buy' in message.text:
                    side = 1
                    side_name = 'BUY'
                elif 'Sell' in message.text:
                    side = 2
                    side_name = 'SELL'
                else:
                    self.bot.reply_to(message, "❌ Invalid selection. Please use the keyboard buttons.")
                    return

                # Get active config name
                try:
                    list_response = requests.get(f"{self.api_url}/config/{user_id}/list")
                    active_config = 'default'
                    if list_response.status_code == 200:
                        data = list_response.json()
                        active_config = data.get('active_config', 'default')

                    # Update side
                    update_data = {'side': side}
                    response = requests.post(f"{self.api_url}/config/{user_id}/{active_config}", json=update_data)

                    if response.status_code == 200:
                        self.bot.reply_to(message, f"✅ Trade side set to: *{side_name}*", parse_mode='Markdown')
                    else:
                        self.bot.reply_to(message, "❌ Error updating trade side.")

                except Exception as e:
                    logger.error(f"Error setting side: {e}")
                    self.bot.reply_to(message, "❌ Error setting trade side.")

                # Clear keyboard and state
                markup = types.ReplyKeyboardRemove()
                self.bot.send_message(message.chat.id, "Keyboard removed.", reply_markup=markup)
                del self.user_states[user_id]

    def send_notification(self, user_id, message):
        """Send notification to user"""
        try:
            self.bot.send_message(user_id, message, parse_mode='Markdown')
            logger.info(f"Notification sent to user {user_id}")
        except Exception as e:
            logger.error(f"Error sending notification to {user_id}: {e}")

    def start_polling(self):
        """Start the bot"""
        logger.info("Starting Telegram bot polling...")
        self.bot.polling(none_stop=True, interval=1)

def main():
    """Main function to run the bot"""
    # Get configuration from environment variables
    telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
    api_url = os.getenv('CONFIG_API_URL', 'http://localhost:5000')

    if not telegram_token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set!")
        print("Please set TELEGRAM_BOT_TOKEN environment variable")
        print("Get token from @BotFather on Telegram")
        return

    logger.info(f"Starting Trading Config Bot with API URL: {api_url}")

    bot = TradingConfigBot(telegram_token, api_url)

    # Start polling
    try:
        bot.start_polling()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot error: {e}")

if __name__ == '__main__':
    main()