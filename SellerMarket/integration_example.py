# Integration Example: Adding Remote Config to Existing Trading Bot

"""
This example shows how to integrate the remote configuration system
into your existing trading bot while maintaining full local fallback.
"""

import os
import sys
from datetime import datetime
from remote_config_client import RemoteConfigClient

def integrate_remote_config():
    """
    Example of how to integrate remote configuration into existing trading bot.
    This replaces manual config.ini reading with remote config that falls back to local.
    """

    # Configuration - set these environment variables or hardcode for testing
    TELEGRAM_USER_ID = os.getenv('TELEGRAM_USER_ID', '123456789')  # Your Telegram user ID
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')  # From @BotFather
    CONFIG_API_URL = os.getenv('CONFIG_API_URL', 'http://localhost:5000')

    # Create remote config client with local fallback
    config_client = RemoteConfigClient(
        api_url=CONFIG_API_URL,
        user_id=TELEGRAM_USER_ID,
        telegram_token=TELEGRAM_BOT_TOKEN,
        local_config_path='config.ini',  # Your existing config file
        local_results_path='order_results.json'  # Your existing results file
    )

    print("🤖 Trading Bot with Remote Configuration")
    print("=" * 50)

    # Check system status
    status = config_client.get_status()
    print(f"📊 System Status:")
    print(f"  API Available: {'✅' if status['api_available'] else '❌'}")
    print(f"  Local Config: {'✅' if status['local_config_exists'] else '❌'}")
    print(f"  Telegram: {'✅' if status['telegram_enabled'] else '❌'}")
    print()

    # Get configuration (remote with local fallback)
    print("⚙️ Loading Configuration...")
    config = config_client.get_config()

    print(f"  Source: {config.get('source', 'unknown')}")
    print(f"  Broker: {config.get('broker', 'N/A')}")
    print(f"  Symbol: {config.get('isin', 'N/A')}")
    print(f"  Side: {'BUY' if config.get('side') == 1 else 'SELL'}")
    print(f"  Username: {config.get('username', 'Not set')}")
    print()

    # Simulate trading logic (replace with your actual trading code)
    print("📈 Executing Trading Logic...")

    # Your existing trading logic here
    # Instead of reading from config.ini directly, use config dict

    username = config.get('username')
    password = config.get('password')
    broker = config.get('broker')
    isin = config.get('isin')
    side = config.get('side')

    if not username or not password:
        print("❌ Credentials not configured!")
        return

    # Simulate order execution
    print(f"  Connecting to broker: {broker}")
    print(f"  Placing {'BUY' if side == 1 else 'SELL'} order for {isin}")

    # Simulate successful order
    order_result = {
        'symbol': isin,
        'side': side,
        'volume': 25000,  # This would come from your calculation logic
        'price': 5700,    # This would come from market data
        'status': 'SUCCESS',
        'order_id': '123456789',
        'timestamp': datetime.now().isoformat()
    }

    print("✅ Order executed successfully!")

    # Save result (both remote and local)
    print("💾 Saving order result...")
    success = config_client.save_order_result(order_result)

    if success:
        print("✅ Order result saved (remote + local)")
    else:
        print("⚠️ Order result saved locally only")

    print()
    print("🎯 Integration Complete!")
    print()
    print("Next steps:")
    print("1. Replace your config.ini reading with RemoteConfigClient")
    print("2. Call save_order_result() after each trade")
    print("3. Set environment variables for production")
    print("4. Start the config API server: python config_api.py")
    print("5. Start the Telegram bot: python telegram_config_bot.py")

def show_migration_example():
    """
    Example of migrating existing config.ini to the remote system.
    """
    print("\n🔄 Config Migration Example")
    print("=" * 30)

    config_client = RemoteConfigClient(user_id='123456789')

    print("Migrating existing config.ini to remote API...")
    success = config_client.migrate_existing_configs()

    if success:
        print("✅ Migration successful!")
        print("Your existing configurations are now available remotely.")
    else:
        print("⚠️ Migration failed - check API connectivity")

def show_environment_setup():
    """
    Show how to set up environment variables for production.
    """
    print("\n🌍 Environment Setup")
    print("=" * 20)

    env_vars = """
# Required Environment Variables

# 1. Telegram Bot Token (get from @BotFather)
export TELEGRAM_BOT_TOKEN="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"

# 2. Your Telegram User ID (send /start to @userinfobot)
export TELEGRAM_USER_ID="123456789"

# 3. Configuration API URL (default: localhost)
export CONFIG_API_URL="http://localhost:5000"

# Example for production server:
export CONFIG_API_URL="http://your-server.com:5000"
"""

    print(env_vars)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "migrate":
            show_migration_example()
        elif command == "env":
            show_environment_setup()
        else:
            print(f"Unknown command: {command}")
            print("Available commands: migrate, env")
    else:
        integrate_remote_config()