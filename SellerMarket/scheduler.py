"""
Simple Scheduler for Trading Bot
Reads scheduler_config.json and executes jobs at scheduled times
"""

import json
import os
import time
import subprocess
import logging
import shlex
from datetime import datetime, timedelta
from threading import Thread, Event
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

def load_locust_config() -> Dict[str, Any]:
    """
    Load Locust configuration from a locust_config.json file located next to this module.
    
    Attempts to read the file and return the value of the top-level "locust" key as a dict. If the file is missing or contains invalid JSON, logs a warning and returns default Locust parameters.
    
    Returns:
        dict: A mapping of Locust parameters. Expected keys include:
            - "users" (int): number of users, default 10
            - "spawn_rate" (int): spawn rate, default 10
            - "run_time" (str): run time string, default "30s"
            - "host" (str): target host URL, default "https://abc.com"
    """
    locust_config_file = os.path.join(os.path.dirname(__file__), 'locust_config.json')
    try:
        with open(locust_config_file, 'r') as f:
            config = json.load(f)
        return config.get('locust', {})
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Could not load locust_config.json: {e}. Using defaults.")
        return {
            'users': 10,
            'spawn_rate': 10,
            'run_time': '30s',
            'host': 'https://abc.com'
        }

def build_locust_command_from_config(base_command: str) -> List[str]:
    """
    Builds a complete Locust CLI command as a list of arguments by appending configured parameters from locust_config.json to a base locust invocation.
    
    Parameters:
        base_command (str): The base command string (e.g. "locust -f locustfile.py --headless"); if it does not start with "locust", it is returned unchanged.
    
    Returns:
        full_command_args (List[str]): The combined command arguments including any of `--users`, `--spawn-rate`, `--run-time`, and `--host` present in the Locust config.
    """
    locust_config = load_locust_config()
    
    # Parse base command to check if it's a locust command
    parts = shlex.split(base_command)
    if not parts or parts[0] != 'locust':
        return shlex.split(base_command) if isinstance(base_command, str) else base_command
    
    # Start with base command parts
    command_args = parts.copy()
    
    # Build additional parameters from config as separate list elements
    if 'users' in locust_config:
        command_args.extend(['--users', str(locust_config['users'])])
    
    if 'spawn_rate' in locust_config:
        command_args.extend(['--spawn-rate', str(locust_config['spawn_rate'])])
    
    if 'run_time' in locust_config:
        command_args.extend(['--run-time', str(locust_config['run_time'])])
    
    if 'host' in locust_config:
        command_args.extend(['--host', str(locust_config['host'])])
    
    logger.info(f"Built Locust command from config: {shlex.join(command_args)}")
    return command_args

class JobScheduler:
    """Simple job scheduler that runs in a background thread"""
    
    def __init__(self, config_file: str):
        self.config_file = config_file
        self.stop_event = Event()
        self.thread = None
        self.executed_today = {}  # Track which jobs ran today
        
    def load_config(self) -> Dict[str, Any]:
        """Load scheduler configuration"""
        try:
            if not os.path.exists(self.config_file):
                logger.warning(f"Scheduler config not found: {self.config_file}")
                return {"enabled": False, "jobs": []}
            
            with open(self.config_file, 'r') as f:
                config = json.load(f)
            
            return config
        except Exception as e:
            logger.error(f"Error loading scheduler config: {e}")
            return {"enabled": False, "jobs": []}
    
    def should_run_job(self, job: Dict[str, Any]) -> bool:
        """Check if a job should run now"""
        try:
            if not job.get('enabled', True):
                return False
            
            # Parse job time
            job_time_str = job['time']  # Format: "HH:MM:SS"
            job_time = datetime.strptime(job_time_str, '%H:%M:%S').time()
            
            # Get current time
            now = datetime.now()
            current_time = now.time()
            
            # Create a time window (30 seconds before to 30 seconds after)
            job_datetime = datetime.combine(now.date(), job_time)
            window_start = (job_datetime - timedelta(seconds=30)).time()
            window_end = (job_datetime + timedelta(seconds=30)).time()
            
            # Check if current time is within window
            if window_start <= window_end:
                # Normal case: window doesn't cross midnight
                in_window = window_start <= current_time <= window_end
            else:
                # Midnight crossing case: window spans midnight
                in_window = current_time >= window_start or current_time <= window_end
            
            if not in_window:
                return False
            
            # Check if already executed today
            job_key = f"{job['name']}_{now.date().isoformat()}"
            if job_key in self.executed_today:
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error checking job schedule: {e}")
            return False
    
    def execute_job(self, job: Dict[str, Any]):
        """
        Run a scheduled job command if it meets validation and record its execution for today.
        
        This method accepts a job mapping with keys "name" and "command", optionally rewrites Locust commands using locust configuration, validates that the command is non-empty and the executable is in a small whitelist, records the job as executed for the current date to prevent duplicate runs, and executes the command as a subprocess with a 10-minute timeout. Outcomes (success, failure, timeout, or validation errors) are logged. Any exceptions during execution are caught and logged; the method does not raise.
        
        Parameters:
            job (Dict[str, Any]): Job definition containing:
                - "name" (str): Human-readable job name used in logs and as part of the once-per-day key.
                - "command" (str | Sequence[str]): Command to execute; if a string beginning with "locust", the command may be expanded from locust_config.json.
        
        Side effects:
            - Updates self.executed_today to mark the job as run for today's date.
            - Spawns a subprocess to run the validated command.
            - Writes informational, debug, and error logs describing validation and execution results.
        """
        try:
            job_name = job['name']
            command = job['command']
            
            # If this is a locust command, build full command from locust_config.json
            if isinstance(command, str) and command.strip().startswith('locust'):
                command = build_locust_command_from_config(command)
            
            # Convert command to list format for logging and validation
            if isinstance(command, str):
                command_for_logging = command
                try:
                    parsed_command = shlex.split(command)
                except ValueError as e:
                    logger.error(f"❌ Invalid command syntax for job '{job_name}': {e}")
                    return
            else:
                # command is already a list from build_locust_command_from_config
                parsed_command = command
                command_for_logging = shlex.join(command)
            
            logger.info(f"⏰ Executing scheduled job: {job_name}")
            logger.info(f"   Command: {command_for_logging}")
            
            # Validate and parse command
            allowed_binaries = {'python', 'locust'}  # Whitelist of allowed executables
            
            if not parsed_command:
                logger.error(f"❌ Empty command for job '{job_name}'")
                return
            
            # Validate executable is in whitelist
            executable = parsed_command[0]
            if executable not in allowed_binaries:
                logger.error(f"❌ Command '{executable}' not in allowed binaries for job '{job_name}'")
                return
            
            # Mark job as executed BEFORE running to prevent multiple attempts on the same day.
            # This is intentional: scheduled jobs (cache warmup, trading) should only run once per day
            # regardless of success/failure. Failures are logged but don't trigger retries.
            now = datetime.now()
            job_key = f"{job_name}_{now.date().isoformat()}"
            self.executed_today[job_key] = now
            
            # Execute command with current environment variables (shell=False for security)
            result = subprocess.run(
                parsed_command,
                shell=False,
                cwd=os.path.dirname(__file__),
                capture_output=True,
                text=True,
                timeout=600,  # 10 minutes max
                env=os.environ.copy()  # Pass environment variables to subprocess
            )
            
            if result.returncode == 0:
                logger.info(f"✅ Job '{job_name}' completed successfully")
                logger.debug(f"Output: {result.stdout[-500:]}")
            else:
                logger.error(f"❌ Job '{job_name}' failed with return code {result.returncode}")
                logger.error(f"Error: {result.stderr[-500:]}")
                
        except subprocess.TimeoutExpired:
            logger.error(f"⏱️ Job '{job_name}' timed out after 10 minutes")
        except Exception as e:
            logger.error(f"❌ Error executing job '{job_name}': {e}")
    
    def run(self):
        """Main scheduler loop"""
        logger.info("📅 Scheduler started")
        
        while not self.stop_event.is_set():
            try:
                # Load current configuration
                config = self.load_config()
                
                if not config.get('enabled', False):
                    # Sleep for 60 seconds if disabled
                    time.sleep(60)
                    continue
                
                # Check each job
                jobs = config.get('jobs', [])
                for job in jobs:
                    if self.should_run_job(job):
                        self.execute_job(job)
                
                # Clean up old execution records (older than today)
                today = datetime.now().date().isoformat()
                self.executed_today = {
                    k: v for k, v in self.executed_today.items() 
                    if k.endswith(today)
                }
                
                # Sleep for 10 seconds before next check
                time.sleep(10)
                
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                time.sleep(60)
        
        logger.info("📅 Scheduler stopped")
    
    def start(self):
        """Start scheduler in background thread"""
        if self.thread and self.thread.is_alive():
            logger.warning("Scheduler already running")
            return
        
        self.stop_event.clear()
        self.thread = Thread(target=self.run, daemon=True, name="JobScheduler")
        self.thread.start()
        logger.info("📅 Scheduler thread started")
    
    def stop(self):
        """Stop scheduler"""
        if self.thread and self.thread.is_alive():
            logger.info("Stopping scheduler...")
            self.stop_event.set()
            self.thread.join(timeout=5)
            logger.info("📅 Scheduler stopped")
    
    def reload_config(self):
        """
        Force the scheduler to pick up configuration changes.
        
        This method trims the executed_today cache to retain only today's execution records,
        removing entries from previous days. This allows the scheduler loop—which already
        rereads the config each iteration (every 10 seconds)—to immediately reflect config
        changes while keeping execution tracking accurate for the current day.
        
        The run loop already rereads the config file on each iteration (every 10 seconds),
        so this method just ensures that execution tracking is up-to-date.
        """
        today = datetime.now().date().isoformat()
        # Keep only jobs that were already executed today
        self.executed_today = {
            k: v for k, v in self.executed_today.items()
            if k.endswith(today)
        }
        logger.info("📅 Scheduler configuration reloaded, execution cache refreshed")