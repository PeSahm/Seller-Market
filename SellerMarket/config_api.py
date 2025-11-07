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
        """
        Initialize the ConfigManager and load persisted configurations and order results.
        
        Parameters:
            config_file (str): Filesystem path to the JSON file that stores user configurations.
            results_file (str): Filesystem path to the JSON file that stores order results.
        """
        self.config_file = config_file
        self.results_file = results_file
        self.load_configs()
        self.load_results()
        logger.info("ConfigManager initialized")

    def load_configs(self):
        """
        Populate the manager's configurations from the configured JSON file.
        
        Reads JSON from self.config_file into self.configs. If the file does not exist or cannot be read/parsed, initializes self.configs to an empty dict.
        """
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
        """
        Persist the in-memory configurations to the configured JSON file.
        
        Writes `self.configs` to `self.config_file` as UTF-8 JSON with indentation. On failure the exception is logged and not propagated.
        """
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.configs, f, indent=2, ensure_ascii=False)
            logger.info("Configurations saved successfully")
        except Exception as e:
            logger.error(f"Error saving configs: {e}")

    def load_results(self):
        """
        Load persisted order results into self.results.
        
        If the results file exists, assigns its JSON contents to self.results; if the file is missing or an error occurs while reading/parsing, sets self.results to an empty list.
        """
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
        """
        Persist the in-memory order results to the configured JSON results file.
        
        Writes the current contents of self.results to self.results_file in JSON format. Failures are handled internally and logged; this method does not raise exceptions to callers.
        """
        try:
            with open(self.results_file, 'w', encoding='utf-8') as f:
                json.dump(self.results, f, indent=2, ensure_ascii=False)
            logger.info("Order results saved successfully")
        except Exception as e:
            logger.error(f"Error saving results: {e}")

    def get_config(self, user_id, config_name=None):
        """
        Retrieve the appropriate configuration for a given user.
        
        If `config_name` is provided, returns that named configuration; otherwise returns the user's active configuration if set, the first available configuration for the user, or the default configuration template when none exist.
        
        Parameters:
            user_id: Identifier of the user whose configuration is requested. Will be converted to a string.
            config_name (optional): Name of a specific configuration to retrieve.
        
        Returns:
            dict: The resolved configuration dictionary (named config, active config, first user config, or the default config template).
        """
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
        """
        Set a single configuration key for a user's named configuration and persist the change.
        
        If the user or the named configuration does not exist, they are created using the default configuration template. The configuration's `updated_at` timestamp is refreshed and the updated configurations are saved to disk.
        
        Parameters:
            user_id (str | int): Identifier of the user whose configuration will be updated.
            config_name (str): Name of the configuration to update.
            key (str): Configuration field name to set.
            value: New value to assign to the specified configuration field.
        """
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
        """
        Set the specified configuration as the active configuration for a user.
        
        If the user does not exist in the stored configurations, a new entry is created. The `user_id` is converted to a string and the change is persisted to disk.
        
        Parameters:
            user_id: Identifier of the user; will be converted to a string.
            config_name (str): Name of the configuration to mark as active.
        """
        user_id = str(user_id)
        if user_id not in self.configs:
            self.configs[user_id] = {}

        self.configs[user_id]['active_config'] = config_name
        self.save_configs()
        logger.info(f"Set active config to {config_name} for user {user_id}")

    def list_configs(self, user_id):
        """
        Return the list of configuration names for a user and the user's active configuration.
        
        Parameters:
            user_id: Identifier of the user; it will be converted to a string for lookup.
        
        Returns:
            dict: {
                'configs': list of configuration names (strings) excluding the 'active_config' entry,
                'active_config': the name of the active configuration for the user, or None if not set
            }
        """
        user_id = str(user_id)
        user_configs = self.configs.get(user_id, {})
        config_names = [k for k in user_configs.keys() if k != 'active_config']
        active_config = user_configs.get('active_config')
        return {
            'configs': config_names,
            'active_config': active_config
        }

    def add_order_result(self, user_id, result):
        """
        Add a new order result for a user and persist it.
        
        Parameters:
            user_id (str|int): Identifier of the user; converted to a string for storage.
            result (Any): Result data to associate with the order (typically a dict).
        
        Returns:
            result_entry (dict): The stored entry containing 'user_id', ISO8601 'timestamp', and the provided 'result'.
        """
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
        """
        Return the most recent order results for a user.
        
        Parameters:
            user_id: Identifier of the user whose results to retrieve.
            limit (int): Maximum number of results to return (defaults to 10).
        
        Returns:
            list: A list of result dictionaries for the given user, containing up to `limit` entries and ordered from oldest to newest within the returned slice.
        """
        user_id = str(user_id)
        user_results = [r for r in self.results if r['user_id'] == user_id]
        return user_results[-limit:]  # Return last N results

    def get_default_config(self):
        """
        Return a default user configuration template.
        
        Returns:
            dict: A configuration dictionary with the following keys:
                - 'username' (str): empty username placeholder.
                - 'password' (str): empty password placeholder.
                - 'broker' (str): default broker identifier ('gs').
                - 'isin' (str): default instrument identifier.
                - 'side' (int): default side value (1).
                - 'created_at' (str): ISO-8601 timestamp of creation.
                - 'updated_at' (str): ISO-8601 timestamp of last update.
        """
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
        """
        Migrate sections from an INI file into the user's JSON-backed configurations.
        
        Reads the given INI file, converts each non-commented section into a configuration merged with the default template, coerces the `side` field to an integer when present, stores the migrated configs under the given user, sets the first migrated config as the user's active configuration if none is set, persists changes to disk, and returns the result of the operation.
        
        Parameters:
        	user_id (str|int): Identifier for the user whose configurations will receive the migrated entries.
        	config_ini_path (str): Path to the INI file to read (default: 'config.ini').
        
        Returns:
        	bool: `True` if one or more configurations were successfully migrated and saved, `False` if the INI file was missing or a migration error occurred.
        """
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
    """
    Provide a current health snapshot of the API server.
    
    Returns:
        flask.Response: JSON object with keys:
            - `status`: service status string (e.g., "healthy").
            - `timestamp`: ISO 8601 timestamp of the snapshot.
            - `configs_count`: number of stored user configurations.
            - `results_count`: number of stored order results.
    """
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
    """
    Update the named or default configuration for a user using the request JSON body.
    
    Expects a JSON payload where `config_name` (optional) selects the target configuration (defaults to "default") and all other top-level keys are applied as configuration fields to that named config. Each provided key/value pair is persisted for the given `user_id`.
    
    Parameters:
        user_id (str): Identifier of the user whose configuration will be updated.
    
    Returns:
        response (flask.Response): JSON response `{"status": "success"}` on successful update.
    """
    data = request.json
    config_name = data.get('config_name', 'default')

    for key, value in data.items():
        if key != 'config_name':
            config_manager.update_config(user_id, config_name, key, value)

    return jsonify({'status': 'success'})

@app.route('/config/<user_id>/<config_name>', methods=['POST'])
def update_specific_config(user_id, config_name):
    """
    Update the named configuration for a user by applying key/value pairs from the request JSON.
    
    Reads the incoming request JSON and updates each provided key into the specified user's configuration, persisting the changes.
    
    Parameters:
        user_id: Identifier of the user whose configuration will be updated.
        config_name: Name of the configuration to update.
    
    Returns:
        dict: JSON object with key `status` set to `'success'` on completion.
    """
    data = request.json
    for key, value in data.items():
        config_manager.update_config(user_id, config_name, key, value)
    return jsonify({'status': 'success'})

@app.route('/config/<user_id>/active/<config_name>', methods=['POST'])
def set_active_config(user_id, config_name):
    """
    Set the user's active configuration to the specified config name and persist the change.
    
    Parameters:
        user_id: Identifier of the user whose active configuration will be set.
        config_name: Name of the configuration to set as active.
    
    Returns:
        A Flask JSON response containing `{'status': 'success'}`.
    """
    config_manager.set_active_config(user_id, config_name)
    return jsonify({'status': 'success'})

@app.route('/config/<user_id>/list', methods=['GET'])
def list_configs(user_id):
    """
    Provide a JSON listing of a user's configurations and the active configuration.
    
    Parameters:
        user_id: Identifier of the user whose configurations are requested.
    
    Returns:
        JSON response containing:
          - `configs`: list of configuration names for the user.
          - `active_config`: the name of the user's active configuration, or `None` if not set.
    """
    configs_info = config_manager.list_configs(user_id)
    return jsonify(configs_info)

@app.route('/results/<user_id>', methods=['GET'])
def get_results(user_id):
    """
    Retrieve recent order results for the specified user.
    
    Parameters:
        user_id (str|int): Identifier of the user whose order results to fetch. The request may include a `limit` query parameter to restrict the number of returned results (defaults to 10).
    
    Returns:
        list: A list of order result entries (dictionaries) for the user, ordered from oldest to newest within the returned slice.
    """
    limit = int(request.args.get('limit', 10))
    results = config_manager.get_order_results(user_id, limit)
    return jsonify(results)

@app.route('/results/<user_id>', methods=['POST'])
def add_result(user_id):
    """
    Add a new order result for the given user and return the index of the stored entry.
    
    Parameters:
        user_id (str|int): Identifier of the user to associate the result with; converted to string internally.
    
    Returns:
        dict: JSON-serializable dictionary with keys:
            - status (str): "success" when the result was added.
            - id (int): Index of the newly added result in the internal results list.
    """
    result = request.json
    result_entry = config_manager.add_order_result(user_id, result)
    return jsonify({'status': 'success', 'id': len(config_manager.results) - 1})

@app.route('/migrate/<user_id>', methods=['POST'])
def migrate_configs(user_id):
    """
    Attempt to migrate INI-format configurations for the given user into the API's JSON-backed store.
    
    Parameters:
        user_id (str): Identifier of the user whose INI configurations should be migrated.
    
    Returns:
        response (flask.Response): On success, a JSON body `{'status': 'success', 'message': 'Configurations migrated successfully'}` with a 200 status code; on failure, a JSON body `{'status': 'error', 'message': 'Migration failed'}` with a 500 status code.
    """
    success = config_manager.migrate_config_ini(user_id)
    if success:
        return jsonify({'status': 'success', 'message': 'Configurations migrated successfully'})
    else:
        return jsonify({'status': 'error', 'message': 'Migration failed'}), 500

@app.errorhandler(404)
def not_found(error):
    """
    Handle 404 Not Found errors by returning a JSON payload indicating the endpoint was not found.
    
    Parameters:
        error (Exception): The original exception or error information passed by Flask.
    
    Returns:
        tuple: A Flask response (JSON) and the HTTP status code 404.
    """
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    """
    Handle an internal server error by logging the provided error and returning a generic JSON error response.
    
    Parameters:
        error: The exception or error information caught by the application; it will be recorded in logs.
    
    Returns:
        A JSON response body {'error': 'Internal server error'} paired with HTTP status code 500.
    """
    logger.error(f"Internal server error: {error}")
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    logger.info("Starting Remote Configuration API Server...")
    logger.info("Available endpoints:")
    logger.info("  GET  /health - Health check")
    logger.info("  GET  /config/<user_id> - Get user config")
    logger.info("  POST /config/<user_id> - Update user config")
    logger.info("  GET  /config/<user_id>/list - List user configs")
    logger.info("  POST /config/<user_id>/active/<config_name> - Set active config")
    logger.info("  GET  /results/<user_id> - Get order results")
    logger.info("  POST /results/<user_id> - Add order result")
    logger.info("  POST /migrate/<user_id> - Migrate config.ini")

    app.run(host='0.0.0.0', port=5000, debug=True)