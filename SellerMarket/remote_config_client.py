#!/usr/bin/env python3
"""
Remote Configuration Client
Client library for trading bots to fetch configurations remotely with local fallback
"""

import requests
import configparser
import json
import os
import logging
from datetime import datetime
from typing import Dict, Any, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RemoteConfigClient:
    """
    Remote configuration client with local fallback support.

    Features:
    - Fetches configurations from remote API
    - Falls back to local config.ini if API unavailable
    - Saves order results both remotely and locally
    - Sends Telegram notifications
    - Handles multiple named configurations
    """

    def __init__(self,
                 api_url: str = "http://localhost:5000",
                 user_id: Optional[str] = None,
                 telegram_token: Optional[str] = None,
                 local_config_path: str = "config.ini",
                 local_results_path: str = "order_results.json"):
        """
        Initialize the remote config client.

        Args:
            api_url: URL of the configuration API server
            user_id: Telegram user ID for configuration management
            telegram_token: Telegram bot token for notifications
            local_config_path: Path to local config.ini fallback file
            local_results_path: Path to local order results file
        """
        self.api_url = api_url.rstrip('/')
        self.user_id = user_id
        self.telegram_token = telegram_token
        self.local_config_path = local_config_path
        self.local_results_path = local_results_path

        # Test API connectivity
        self.api_available = self._test_api_connectivity()
        if self.api_available:
            logger.info("✅ Remote API available")
        else:
            logger.warning("⚠️ Remote API not available, using local fallback")

    def _test_api_connectivity(self) -> bool:
        """Test if the remote API is accessible."""
        try:
            response = requests.get(f"{self.api_url}/health", timeout=5)
            return response.status_code == 200
        except:
            return False

    def get_config(self, config_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Get configuration from remote API with local fallback.

        Args:
            config_name: Specific configuration name, or None for active config

        Returns:
            Configuration dictionary
        """
        # Try remote API first
        if self.api_available and self.user_id:
            try:
                params = {'config': config_name} if config_name else {}
                response = requests.get(
                    f"{self.api_url}/config/{self.user_id}",
                    params=params,
                    timeout=10
                )

                if response.status_code == 200:
                    remote_config = response.json()
                    logger.info("✅ Loaded config from remote API")
                    return remote_config

            except Exception as e:
                logger.warning(f"⚠️ Remote config failed: {e}")

        # Fallback to local config
        logger.info("📁 Using local config fallback")
        return self._load_local_config()

    def _load_local_config(self) -> Dict[str, Any]:
        """
        Load configuration from local config.ini file.

        Returns:
            Configuration dictionary compatible with API format
        """
        try:
            if not os.path.exists(self.local_config_path):
                logger.warning(f"Local config file not found: {self.local_config_path}")
                return self._get_default_config()

            config = configparser.ConfigParser()
            config.read(self.local_config_path, encoding='utf-8')

            # Find first non-comment section
            for section_name in config.sections():
                if not section_name.startswith('#'):
                    section_data = dict(config[section_name])

                    # Convert types to match API format
                    if 'side' in section_data:
                        section_data['side'] = int(section_data.get('side', 1))

                    # Add metadata
                    section_data['source'] = 'local_fallback'
                    section_data['config_name'] = section_name

                    logger.info(f"📁 Loaded local config section: {section_name}")
                    return section_data

            logger.warning("No valid config sections found in local file")
            return self._get_default_config()

        except Exception as e:
            logger.error(f"Error loading local config: {e}")
            return self._get_default_config()

    def _get_default_config(self) -> Dict[str, Any]:
        """Get default configuration."""
        return {
            'username': '',
            'password': '',
            'broker': 'gs',
            'isin': 'IRO1MHRN0001',
            'side': 1,
            'source': 'default_fallback',
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat()
        }

    def save_order_result(self, result: Dict[str, Any]) -> bool:
        """
        Save order result both remotely and locally.

        Args:
            result: Order result dictionary

        Returns:
            True if saved successfully (at least locally)
        """
        success = False

        # Try remote API first
        if self.api_available and self.user_id:
            try:
                response = requests.post(
                    f"{self.api_url}/results/{self.user_id}",
                    json=result,
                    timeout=10
                )
                if response.status_code == 200:
                    logger.info("✅ Order result saved to remote API")
                    success = True
                else:
                    logger.warning("⚠️ Failed to save result to remote API")
            except Exception as e:
                logger.warning(f"⚠️ Remote result save failed: {e}")

        # Always save locally as backup
        local_saved = self._save_result_locally(result)
        if local_saved:
            logger.info("✅ Order result saved locally")
            success = True

        # Send notification if possible
        if success:
            self.send_notification(self._format_order_notification(result))

        return success

    def _save_result_locally(self, result: Dict[str, Any]) -> bool:
        """
        Save order result to local JSON file.

        Args:
            result: Order result dictionary

        Returns:
            True if saved successfully
        """
        try:
            # Load existing results
            existing_results = []
            if os.path.exists(self.local_results_path):
                try:
                    with open(self.local_results_path, 'r', encoding='utf-8') as f:
                        existing_results = json.load(f)
                except:
                    existing_results = []

            # Add new result with metadata
            result_entry = {
                'user_id': self.user_id or 'local',
                'timestamp': datetime.now().isoformat(),
                'result': result,
                'source': 'local_save'
            }
            existing_results.append(result_entry)

            # Save back to file
            with open(self.local_results_path, 'w', encoding='utf-8') as f:
                json.dump(existing_results, f, indent=2, ensure_ascii=False)

            return True

        except Exception as e:
            logger.error(f"Error saving result locally: {e}")
            return False

    def send_notification(self, message: str) -> bool:
        """
        Send Telegram notification.

        Args:
            message: Notification message

        Returns:
            True if sent successfully
        """
        if not self.telegram_token or not self.user_id:
            return False

        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            data = {
                'chat_id': self.user_id,
                'text': message,
                'parse_mode': 'Markdown'
            }
            response = requests.post(url, json=data, timeout=10)

            if response.status_code == 200:
                logger.info("✅ Notification sent successfully")
                return True
            else:
                logger.warning(f"⚠️ Notification failed: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"Error sending notification: {e}")
            return False

    def _format_order_notification(self, result: Dict[str, Any]) -> str:
        """
        Format order result for Telegram notification.

        Args:
            result: Order result dictionary

        Returns:
            Formatted notification message
        """
        status_emoji = "✅" if result.get('status') == 'SUCCESS' else "❌"
        side = "BUY" if result.get('side') == 1 else "SELL"

        message = f"""
{status_emoji} *Order Result*

📈 *Symbol:* `{result.get('symbol', 'N/A')}`
📊 *Side:* {side}
📦 *Volume:* {result.get('volume', 'N/A'):,} shares
💰 *Price:* {result.get('price', 'N/A'):,} Rials
🕒 *Time:* {datetime.now().strftime('%H:%M:%S')}
        """.strip()

        return message

    def migrate_existing_configs(self) -> bool:
        """
        Migrate existing config.ini to remote API.

        Returns:
            True if migration successful
        """
        if not self.api_available or not self.user_id:
            logger.warning("Cannot migrate: API not available or no user_id")
            return False

        try:
            response = requests.post(f"{self.api_url}/migrate/{self.user_id}")
            if response.status_code == 200:
                logger.info("✅ Config migration successful")
                return True
            else:
                logger.warning("⚠️ Config migration failed")
                return False
        except Exception as e:
            logger.error(f"Error migrating configs: {e}")
            return False

    def get_status(self) -> Dict[str, Any]:
        """
        Get system status information.

        Returns:
            Status dictionary
        """
        status = {
            'api_available': self.api_available,
            'local_config_exists': os.path.exists(self.local_config_path),
            'local_results_exists': os.path.exists(self.local_results_path),
            'telegram_enabled': bool(self.telegram_token and self.user_id),
            'timestamp': datetime.now().isoformat()
        }

        # Try to get API health
        if self.api_available:
            try:
                response = requests.get(f"{self.api_url}/health", timeout=5)
                if response.status_code == 200:
                    api_health = response.json()
                    status.update({
                        'api_configs_count': api_health.get('configs_count', 0),
                        'api_results_count': api_health.get('results_count', 0)
                    })
            except:
                status['api_available'] = False

        return status

# Convenience functions for easy integration
def create_config_client(user_id=None, telegram_token=None, api_url="http://localhost:5000"):
    """
    Create a configured RemoteConfigClient instance.

    Args:
        user_id: Telegram user ID
        telegram_token: Telegram bot token
        api_url: Configuration API URL

    Returns:
        Configured RemoteConfigClient instance
    """
    return RemoteConfigClient(
        api_url=api_url,
        user_id=user_id,
        telegram_token=telegram_token
    )

# Example usage in trading bot
if __name__ == "__main__":
    # Example usage
    client = RemoteConfigClient(
        user_id="123456789",  # Your Telegram user ID
        telegram_token="YOUR_BOT_TOKEN",  # From @BotFather
        api_url="http://localhost:5000"
    )

    # Get configuration (remote with local fallback)
    config = client.get_config()
    print(f"Loaded config: {config}")

    # Save order result (remote + local)
    order_result = {
        'symbol': 'IRO1MHRN0001',
        'side': 1,
        'volume': 25000,
        'price': 5700,
        'status': 'SUCCESS'
    }
    client.save_order_result(order_result)

    # Check system status
    status = client.get_status()
    print(f"System status: {status}")