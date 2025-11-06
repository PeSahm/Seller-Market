# Remote Configuration System for Trading Bot

## Overview

A Telegram bot-based remote configuration system that allows you to manage multiple trading bot configurations without manual file editing, with real-time notifications for order results.

## Architecture

### Components

1. **Telegram Bot** - Remote configuration interface + order notifications
2. **Configuration API** - REST API for managing multiple configs
3. **Storage Backend** - JSON storage for configurations + order results
4. **Trading Bot Integration** - Modified to fetch configs and send notifications

### Features

- ✅ **Multi-config support** - Manage multiple named configurations (like [Mostafa_gs_], [Order_Account2_BBI])
- ✅ **Real-time notifications** - Get order results via Telegram
- ✅ **File-based results** - Integrates with your existing result saving mechanism
- ✅ **Audit logging** - Track all configuration and order changes
- ✅ **Security** - Telegram authentication + encrypted storage

## Implementation Plan

### Phase 1: Enhanced Telegram Bot Setup

```python
# telegram_config_bot.py
import telebot
from telebot import types
import requests
import json

class TradingConfigBot:
    def __init__(self, token, api_url):
        self.bot = telebot.TeleBot(token)
        self.api_url = api_url
        self.setup_handlers()

    def setup_handlers(self):
        @self.bot.message_handler(commands=['start'])
        def send_welcome(self, message):
            # Welcome message with available commands

        @self.bot.message_handler(commands=['list_configs'])
        def list_configs(self, message):
            # List all available configurations

        @self.bot.message_handler(commands=['select_config'])
        def select_config(self, message):
            # Select active configuration by name

        @self.bot.message_handler(commands=['set_broker'])
        def set_broker(self, message):
            # Set broker for selected config

        @self.bot.message_handler(commands=['set_credentials'])
        def set_credentials(self, message):
            # Set username/password securely for selected config

        @self.bot.message_handler(commands=['set_symbol'])
        def set_symbol(self, message):
            # Set ISIN/symbol for selected config

        @self.bot.message_handler(commands=['set_side'])
        def set_side(self, message):
            # Set buy/sell side for selected config

        @self.bot.message_handler(commands=['get_config'])
        def get_config(self, message):
            # Show current active configuration

        @self.bot.message_handler(commands=['get_results'])
        def get_results(self, message):
            # Show recent order results

        @self.bot.message_handler(commands=['status'])
        def get_status(self, message):
            # Show bot status and last run
```

### Phase 2: Enhanced Configuration API

```python
# config_api.py
from flask import Flask, request, jsonify
import json
import os
from datetime import datetime

app = Flask(__name__)

class ConfigManager:
    def __init__(self, config_file='remote_configs.json', results_file='order_results.json'):
        self.config_file = config_file
        self.results_file = results_file
        self.load_configs()
        self.load_results()

    def load_configs(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                self.configs = json.load(f)
        else:
            self.configs = {}

    def save_configs(self):
        with open(self.config_file, 'w') as f:
            json.dump(self.configs, f, indent=2)

    def load_results(self):
        if os.path.exists(self.results_file):
            with open(self.results_file, 'r') as f:
                self.results = json.load(f)
        else:
            self.results = []

    def save_results(self):
        with open(self.results_file, 'w') as f:
            json.dump(self.results, f, indent=2)

    def get_config(self, user_id, config_name=None):
        user_configs = self.configs.get(str(user_id), {})
        if config_name:
            return user_configs.get(config_name, self.get_default_config())
        # Return active config or first available
        active_config = user_configs.get('active_config')
        if active_config and active_config in user_configs:
            return user_configs[active_config]
        return list(user_configs.values())[0] if user_configs else self.get_default_config()

    def update_config(self, user_id, config_name, key, value):
        user_id = str(user_id)
        if user_id not in self.configs:
            self.configs[user_id] = {}

        if config_name not in self.configs[user_id]:
            self.configs[user_id][config_name] = self.get_default_config()

        self.configs[user_id][config_name][key] = value
        self.configs[user_id][config_name]['updated_at'] = datetime.now().isoformat()
        self.save_configs()

    def set_active_config(self, user_id, config_name):
        user_id = str(user_id)
        if user_id not in self.configs:
            self.configs[user_id] = {}
        self.configs[user_id]['active_config'] = config_name
        self.save_configs()

    def add_order_result(self, user_id, result):
        result_entry = {
            'user_id': str(user_id),
            'timestamp': datetime.now().isoformat(),
            'result': result
        }
        self.results.append(result_entry)
        self.save_results()
        return result_entry

    def get_default_config(self):
        return {
            'username': '',
            'password': '',
            'broker': 'gs',
            'isin': 'IRO1MHRN0001',
            'side': 1,
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat()
        }

config_manager = ConfigManager()

@app.route('/config/<user_id>', methods=['GET'])
def get_config(user_id):
    config_name = request.args.get('config')
    config = config_manager.get_config(user_id, config_name)
    return jsonify(config)

@app.route('/config/<user_id>/<config_name>', methods=['POST'])
def update_config(user_id, config_name):
    data = request.json
    for key, value in data.items():
        config_manager.update_config(user_id, config_name, key, value)
    return jsonify({'status': 'success'})

@app.route('/config/<user_id>/active/<config_name>', methods=['POST'])
def set_active_config(user_id, config_name):
    config_manager.set_active_config(user_id, config_name)
    return jsonify({'status': 'success'})

@app.route('/results/<user_id>', methods=['GET'])
def get_results(user_id):
    user_results = [r for r in config_manager.results if r['user_id'] == str(user_id)]
    return jsonify(user_results[-10:])  # Last 10 results

@app.route('/results/<user_id>', methods=['POST'])
def add_result(user_id):
    result = request.json
    result_entry = config_manager.add_order_result(user_id, result)
    return jsonify({'status': 'success', 'id': len(config_manager.results) - 1})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
```

### Phase 3: Trading Bot Integration with Notifications

```python
# Modified api_client.py or main bot file
import requests
import configparser

class RemoteConfigClient:
    def __init__(self, api_url, user_id, telegram_bot_token=None):
        self.api_url = api_url
        self.user_id = user_id
        self.telegram_token = telegram_bot_token

    def get_config(self, config_name=None):
        try:
            params = {'config': config_name} if config_name else {}
            response = requests.get(f"{self.api_url}/config/{self.user_id}", params=params)
            if response.status_code == 200:
                return response.json()
            else:
                return self.load_local_config()
        except:
            return self.load_local_config()

    def load_local_config(self):
        # Fallback to local config.ini - supports multiple sections
        config = configparser.ConfigParser()
        config.read('config.ini')
        # Return first non-comment section
        for section in config.sections():
            if not section.startswith('#'):
                return dict(config[section])
        return {}

    def send_notification(self, message):
        if self.telegram_token:
            try:
                url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
                data = {
                    'chat_id': self.user_id,
                    'text': message,
                    'parse_mode': 'Markdown'
                }
                requests.post(url, json=data)
            except:
                pass  # Silent fail if notification fails

    def save_order_result(self, result):
        try:
            response = requests.post(f"{self.api_url}/results/{self.user_id}", json=result)
            if response.status_code == 200:
                # Also save to local file (your existing mechanism)
                self.save_to_local_file(result)
                # Send notification
                self.send_notification(f"📊 Order Result: {result}")
        except:
            # Fallback to local saving only
            self.save_to_local_file(result)

    def save_to_local_file(self, result):
        # Your existing file saving mechanism
        import json
        try:
            with open('order_results.json', 'a') as f:
                json.dump(result, f)
                f.write('\n')
        except:
            pass

# Usage in trading bot
config_client = RemoteConfigClient(
    "http://localhost:5000", 
    telegram_user_id, 
    telegram_bot_token
)

# Get active configuration
config = config_client.get_config()

# Your existing trading logic
username = config['username']
broker = config['broker']
# ... rest of your logic

# At the end, save result and notify
order_result = {
    'symbol': config['isin'],
    'side': 'BUY' if config['side'] == 1 else 'SELL',
    'volume': calculated_volume,
    'price': executed_price,
    'status': 'SUCCESS',
    'timestamp': datetime.now().isoformat()
}

config_client.save_order_result(order_result)
```

## Multi-Configuration Support

### Config.ini Structure
Your existing config.ini supports multiple sections:
```ini
[Mostafa_gs_]        # Active config
username = 4580090306
password = Mm@12345
broker = bbi
isin = IRO1MHRN0001
side = 1

[Order_Account2_BBI] # Alternative config
username = YOUR_ACCOUNT_NUMBER
password = YOUR_PASSWORD
broker = bbi
isin = IRO1MHRN0001
side = 1
```

### Telegram Bot Multi-Config Commands
```
/list_configs - Show all available configurations
/select_config Mostafa_gs_ - Switch to specific config
/set_broker gs - Update broker for active config
/get_config - Show current active configuration
```

## Order Result Notifications

### Notification Types
- **Order Placed**: Immediate confirmation when order is submitted
- **Order Executed**: When order is filled (partial or complete)
- **Order Failed**: If order placement fails
- **Daily Summary**: End-of-day summary of all orders

### Notification Format
```
📊 Order Result: SUCCESS
Symbol: IRO1MHRN0001 (فولاد)
Side: BUY
Volume: 25,000 shares
Price: 5,700 Rials
Time: 2025-11-06 08:45:31
```

### Integration with Existing Results
- **File Saving**: Maintains your existing `order_results.json` file
- **API Storage**: Also stores in remote API for Telegram access
- **Dual Persistence**: Both local and remote storage for reliability

## Cron Job Integration

### Enhanced Setup with Multi-Config
```bash
# Modified crontab with config selection
30 8 * * 1-5 /path/to/trading_bot.py --populate-cache --user-id YOUR_TELEGRAM_ID --config Mostafa_gs_
44 8 * * 1-5 timeout 40s /path/to/trading_bot.py --execute-trades --user-id YOUR_TELEGRAM_ID --config Mostafa_gs_
```

### Notification-Enabled Cron Jobs
```bash
# Add notification on completion
45 8 * * 1-5 /path/to/trading_bot.py --send-summary --user-id YOUR_TELEGRAM_ID
```

## Telegram Bot Commands

```
/start - Initialize bot
/list_configs - Show all configurations
/select_config <name> - Switch active configuration
/set_broker <code> - Set broker (gs/bbi/shahr/karamad/tejarat/shams)
/set_credentials - Securely set username/password
/set_symbol <ISIN> - Set stock symbol
/set_side <1|2> - Set buy (1) or sell (2)
/get_config - Show current active configuration
/get_results - Show recent order results (last 10)
/status - Show bot and trading status
/help - Show available commands
```

## Benefits

### For You
- ✅ **Multi-config management** - Handle multiple accounts/configs easily
- ✅ **Real-time notifications** - Get order results instantly via Telegram
- ✅ **Remote management** - Configure from anywhere without file editing
- ✅ **Audit trail** - Track all configuration and order changes
- ✅ **Reliable** - Local fallback + file-based persistence

### For the System
- ✅ **Cron job friendly** - Works perfectly with your 8:30/8:44 schedule
- ✅ **File integration** - Builds on your existing result saving
- ✅ **Scalable** - Easy to add new configurations
- ✅ **Maintainable** - Clean separation of concerns

## Next Steps

1. **Choose deployment option** (local server recommended)
2. **Set up Telegram bot** (get token from @BotFather)
3. **Implement configuration API** (enhanced for multi-config)
4. **Modify trading bot** - Add remote config + notifications
5. **Test with cron jobs** - Ensure timing and notifications work
6. **Migrate existing configs** - Import from config.ini to API

Would you like me to implement any specific part of this enhanced system?
