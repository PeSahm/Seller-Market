#!/usr/bin/env python3
"""
Remote Configuration API Server
Flask-based REST API for managing trading bot configurations and order results
"""

from flask import Flask, request, jsonify
import json
import os
from datetime import datetime
import configparser
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

class ConfigManager:
    def __init__(self, config_file='remote_configs.json', results_file='order_results.json'):
        self.config_file = config_file
        self.results_file = results_file
        self.load_configs()
        self.load_results()
        logger.info("ConfigManager initialized")

    def load_configs(self):
        """Load configurations from JSON file"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    self.configs = json.load(f)
                logger.info(f"Loaded {len(self.configs)} user configurations")
            except Exception as e:
                logger.error(f"Error loading configs: {e}")
                self.configs = {}
        else:
            self.configs = {}
            logger.info("No existing config file found, starting fresh")

    def save_configs(self):
        """Save configurations to JSON file"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.configs, f, indent=2, ensure_ascii=False)
            logger.info("Configurations saved successfully")
        except Exception as e:
            logger.error(f"Error saving configs: {e}")

    def load_results(self):
        """Load order results from JSON file"""
        if os.path.exists(self.results_file):
            try:
                with open(self.results_file, 'r', encoding='utf-8') as f:
                    self.results = json.load(f)
                logger.info(f"Loaded {len(self.results)} order results")
            except Exception as e:
                logger.error(f"Error loading results: {e}")
                self.results = []
        else:
            self.results = []
            logger.info("No existing results file found, starting fresh")

    def save_results(self):
        """Save order results to JSON file"""
        try:
            with open(self.results_file, 'w', encoding='utf-8') as f:
                json.dump(self.results, f, indent=2, ensure_ascii=False)
            logger.info("Order results saved successfully")
        except Exception as e:
            logger.error(f"Error saving results: {e}")

    def get_config(self, user_id, config_name=None):
        """Get configuration for a user"""
        user_id = str(user_id)
        user_configs = self.configs.get(user_id, {})

        if config_name:
            # Return specific config
            return user_configs.get(config_name, self.get_default_config())

        # Return active config or first available
        active_config = user_configs.get('active_config')
        if active_config and active_config in user_configs:
            return user_configs[active_config]

        # Return first available config
        config_names = [k for k in user_configs.keys() if k != 'active_config']
        if config_names:
            return user_configs[config_names[0]]

        return self.get_default_config()

    def update_config(self, user_id, config_name, key, value):
        """Update a specific configuration value"""
        user_id = str(user_id)

        if user_id not in self.configs:
            self.configs[user_id] = {}

        if config_name not in self.configs[user_id]:
            self.configs[user_id][config_name] = self.get_default_config()

        self.configs[user_id][config_name][key] = value
        self.configs[user_id][config_name]['updated_at'] = datetime.now().isoformat()
        self.save_configs()
        logger.info(f"Updated config {config_name} for user {user_id}: {key} = {value}")

    def set_active_config(self, user_id, config_name):
        """Set the active configuration for a user"""
        user_id = str(user_id)
        if user_id not in self.configs:
            self.configs[user_id] = {}

        # Validate that the config_name exists for this user
        if config_name not in self.configs[user_id] or config_name == 'active_config':
            logger.warning(f"Config '{config_name}' does not exist for user {user_id}")
            return False

        self.configs[user_id]['active_config'] = config_name
        self.save_configs()
        logger.info(f"Set active config to {config_name} for user {user_id}")
        return True

    def list_configs(self, user_id):
        """List all configurations for a user"""
        user_id = str(user_id)
        user_configs = self.configs.get(user_id, {})
        config_names = [k for k in user_configs.keys() if k != 'active_config']
        active_config = user_configs.get('active_config')
        return {
            'configs': config_names,
            'active_config': active_config
        }

    def add_order_result(self, user_id, result):
        """Add a new order result"""
        result_entry = {
            'user_id': str(user_id),
            'timestamp': datetime.now().isoformat(),
            'result': result
        }
        self.results.append(result_entry)
        self.save_results()
        logger.info(f"Added order result for user {user_id}")
        return result_entry

    def get_order_results(self, user_id, limit=10):
        """Get recent order results for a user"""
        user_id = str(user_id)
        user_results = [r for r in self.results if r['user_id'] == user_id]
        return user_results[-limit:]  # Return last N results

    def get_default_config(self):
        """Get default configuration template"""
        return {
            'username': '',
            'password': '',
            'broker': 'gs',
            'isin': 'IRO1MHRN0001',
            'side': 1,
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat()
        }

    def migrate_config_ini(self, user_id, config_ini_path='config.ini'):
        """Migrate existing config.ini sections to API"""
        if not os.path.exists(config_ini_path):
            logger.warning(f"Config file {config_ini_path} not found")
            return False

        try:
            config = configparser.ConfigParser()
            config.read(config_ini_path, encoding='utf-8')

            user_id = str(user_id)
            if user_id not in self.configs:
                self.configs[user_id] = {}

            migrated_count = 0
            for section_name in config.sections():
                if not section_name.startswith('#'):  # Skip commented sections
                    section_data = dict(config[section_name])

                    # Convert string values to appropriate types
                    if 'side' in section_data:
                        section_data['side'] = int(section_data['side'])

                    self.configs[user_id][section_name] = {
                        **self.get_default_config(),
                        **section_data
                    }
                    migrated_count += 1

            # Set first config as active if none set
            if migrated_count > 0 and 'active_config' not in self.configs[user_id]:
                first_config = list(self.configs[user_id].keys())[0]
                self.configs[user_id]['active_config'] = first_config

            self.save_configs()
            logger.info(f"Migrated {migrated_count} configurations from {config_ini_path}")
            return True

        except Exception as e:
            logger.error(f"Error migrating config.ini: {e}")
            return False

# Initialize config manager
config_manager = ConfigManager()

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'configs_count': len(config_manager.configs),
        'results_count': len(config_manager.results)
    })

@app.route('/config/<user_id>', methods=['GET'])
def get_config(user_id):
    """Get configuration for a user"""
    config_name = request.args.get('config')
    config = config_manager.get_config(user_id, config_name)
    return jsonify(config)

@app.route('/config/<user_id>', methods=['POST'])
def update_config(user_id):
    """Update configuration for a user"""
    data = request.json
    config_name = data.get('config_name', 'default')

    for key, value in data.items():
        if key != 'config_name':
            config_manager.update_config(user_id, config_name, key, value)

    return jsonify({'status': 'success'})

@app.route('/config/<user_id>/<config_name>', methods=['POST'])
def update_specific_config(user_id, config_name):
    """Update a specific named configuration"""
    data = request.json
    for key, value in data.items():
        config_manager.update_config(user_id, config_name, key, value)
    return jsonify({'status': 'success'})

@app.route('/config/<user_id>/active/<config_name>', methods=['POST'])
def set_active_config(user_id, config_name):
    """Set active configuration for a user"""
    success = config_manager.set_active_config(user_id, config_name)
    if not success:
        return jsonify({'error': f'Configuration "{config_name}" does not exist for user {user_id}'}), 400
    return jsonify({'status': 'success'})

@app.route('/config/<user_id>/list', methods=['GET'])
def list_configs(user_id):
    """List all configurations for a user"""
    configs_info = config_manager.list_configs(user_id)
    return jsonify(configs_info)

@app.route('/results/<user_id>', methods=['GET'])
def get_results(user_id):
    """Get order results for a user"""
    limit = int(request.args.get('limit', 10))
    results = config_manager.get_order_results(user_id, limit)
    return jsonify(results)

@app.route('/results/<user_id>', methods=['POST'])
def add_result(user_id):
    """Add a new order result for a user"""
    result = request.json
    if not result:
        return jsonify({'status': 'error', 'message': 'Invalid or empty payload'}), 400
    config_manager.add_order_result(user_id, result)
    return jsonify({'status': 'success', 'id': len(config_manager.results) - 1})

@app.route('/migrate/<user_id>', methods=['POST'])
def migrate_configs(user_id):
    """Migrate existing config.ini to API"""
    success = config_manager.migrate_config_ini(user_id)
    if success:
        return jsonify({'status': 'success', 'message': 'Configurations migrated successfully'})
    else:
        return jsonify({'status': 'error', 'message': 'Migration failed'}), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    # Server configuration from environment variables (with secure defaults)
    server_host = os.getenv('API_HOST', '127.0.0.1')  # Local-only by default
    server_port = int(os.getenv('API_PORT', '5000'))
    server_debug = os.getenv('API_DEBUG', 'false').lower() == 'true'  # Debug disabled by default
    
    logger.info("Starting Remote Configuration API Server...")
    logger.info(f"Server configuration: host={server_host}, port={server_port}, debug={server_debug}")
    
    if server_host != '127.0.0.1':
        logger.warning(f"⚠️  WARNING: Server bound to {server_host} (not localhost)")
        logger.warning("⚠️  Ensure this is intended for production deployment")
    
    if server_debug:
        logger.warning("⚠️  WARNING: Debug mode enabled - do not use in production!")
        logger.warning("⚠️  Interactive debugger will be available at /debug")
    
    logger.info("Available endpoints:")
    logger.info("  GET  /health - Health check")
    logger.info("  GET  /config/<user_id> - Get user config")
    logger.info("  POST /config/<user_id> - Update user config")
    logger.info("  GET  /config/<user_id>/list - List user configs")
    logger.info("  POST /config/<user_id>/active/<config_name> - Set active config")
    logger.info("  GET  /results/<user_id> - Get order results")
    logger.info("  POST /results/<user_id> - Add order result")
    logger.info("  POST /migrate/<user_id> - Migrate config.ini")

    app.run(host=server_host, port=server_port, debug=server_debug)