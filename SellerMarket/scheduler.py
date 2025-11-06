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
        """Execute a scheduled job"""
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
