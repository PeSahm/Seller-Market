"""
Simple Scheduler for Trading Bot
Reads scheduler_config.json and executes jobs at scheduled times
"""

import json
import os
import time
import subprocess
import logging
from datetime import datetime, timedelta
from threading import Thread, Event
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class JobScheduler:
    """Simple job scheduler that runs in a background thread"""
    
    def __init__(self, config_file: str):
        """
        Initialize the scheduler with the path to its configuration and prepare thread control and execution tracking.
        
        Parameters:
            config_file (str): Path to the JSON scheduler configuration file.
        
        Attributes:
            config_file (str): Stored configuration file path.
            stop_event (threading.Event): Event used to signal the scheduler loop to stop.
            thread (Optional[threading.Thread]): Background thread running the scheduler, or `None` if not started.
            executed_today (dict): Maps per-day job keys to their execution timestamp to prevent multiple runs on the same day.
        """
        self.config_file = config_file
        self.stop_event = Event()
        self.thread = None
        self.executed_today = {}  # Track which jobs ran today
        
    def load_config(self) -> Dict[str, Any]:
        """
        Load scheduler configuration from the configured file.
        
        Reads and parses JSON from self.config_file and returns the resulting configuration. If the file is missing or an error occurs while reading or parsing, returns a default configuration with "enabled": False and an empty "jobs" list; no exceptions are propagated.
        
        Returns:
            config (Dict[str, Any]): Scheduler configuration dictionary; defaults to {"enabled": False, "jobs": []} on missing file or error.
        """
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
        """
        Determine whether the given scheduled job should run at the current time.
        
        Parameters:
            job (Dict[str, Any]): Job configuration with required keys:
                - name (str): Unique job name used to track daily execution.
                - time (str): Scheduled time in "HH:MM:SS" format.
                - enabled (bool, optional): Whether the job is enabled (default True).
        
        Returns:
            bool: `true` if the current local time is within 30 seconds before or after the job's scheduled time and the job has not already been executed today; `false` otherwise. On error, logs the problem and returns `false`.
        """
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
            in_window = window_start <= current_time <= window_end
            
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
        Execute a scheduled job by recording its execution and running its shell command.
        
        Parameters:
            job (Dict[str, Any]): Job definition containing at least:
                - name (str): Human-readable job identifier used for logging and execution tracking.
                - command (str): Shell command to execute.
        
        Details:
            - Records the job as executed for the current date to prevent re-running the same job later the same day.
            - Runs the provided command in a subprocess with a 10-minute timeout, using the module directory as cwd and inheriting the current environment.
            - Logs success or failure and includes a snippet of stdout/stderr for diagnostics.
            - On timeout or other execution errors, logs an error but does not raise exceptions.
        """
        try:
            job_name = job['name']
            command = job['command']
            
            logger.info(f"⏰ Executing scheduled job: {job_name}")
            logger.info(f"   Command: {command}")
            
            # Mark as executed
            now = datetime.now()
            job_key = f"{job_name}_{now.date().isoformat()}"
            self.executed_today[job_key] = now
            
            # Execute command with current environment variables
            result = subprocess.run(
                command,
                shell=True,
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
        """
        Run the scheduler loop that continuously loads configuration and executes jobs when scheduled.
        
        Each iteration reloads the scheduler configuration; if the scheduler is disabled it waits 60 seconds before rechecking. When enabled, it iterates configured jobs and executes those that are due, prunes execution records to keep only today's entries, and waits 10 seconds between checks. Exceptions are logged and cause a 60-second delay before retrying. The loop exits when the scheduler's stop event is set.
        """
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
        """
        Start the scheduler in a background daemon thread.
        
        If the scheduler is already running this method does nothing. The method clears the stop event and starts a daemon thread named "JobScheduler" that runs the scheduler loop.
        """
        if self.thread and self.thread.is_alive():
            logger.warning("Scheduler already running")
            return
        
        self.stop_event.clear()
        self.thread = Thread(target=self.run, daemon=True, name="JobScheduler")
        self.thread.start()
        logger.info("📅 Scheduler thread started")
    
    def stop(self):
        """
        Signal the scheduler to stop and wait briefly for the background thread to terminate.
        
        If the scheduler thread is active, sets the internal stop event and joins the thread with a 5-second timeout.
        """
        if self.thread and self.thread.is_alive():
            logger.info("Stopping scheduler...")
            self.stop_event.set()
            self.thread.join(timeout=5)
            logger.info("📅 Scheduler stopped")