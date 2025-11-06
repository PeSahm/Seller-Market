#!/usr/bin/env python3
"""
Trading Bot Windows Service
Runs Telegram bot, scheduler, and provides manual control
"""

import sys
import os
import time
import logging
import json
import subprocess
from datetime import datetime, time as dt_time
from pathlib import Path
import threading

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

# Configure logging
LOG_DIR = Path(__file__).parent / 'logs'
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'trading_service.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Import service base (install with: pip install pywin32)
try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
    HAS_PYWIN32 = True
except ImportError:
    HAS_PYWIN32 = False
    logger.warning("pywin32 not installed. Service features disabled. Install with: pip install pywin32")


class SchedulerConfig:
    """Manages scheduler configuration"""
    
    def __init__(self, config_file='scheduler_config.json'):
        self.config_file = Path(__file__).parent / config_file
        self.config = self.load_config()
    
    def load_config(self):
        """Load scheduler configuration"""
        default_config = {
            "enabled": True,
            "jobs": [
                {
                    "name": "cache_warmup",
                    "time": "08:30:00",
                    "command": "python cache_warmup.py",
                    "enabled": True
                },
                {
                    "name": "run_trading",
                    "time": "08:44:30",
                    "command": "locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 30s",
                    "enabled": True
                }
            ]
        }
        
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading config: {e}")
                return default_config
        else:
            # Create default config
            self.save_config(default_config)
            return default_config
    
    def save_config(self, config=None):
        """Save scheduler configuration"""
        if config:
            self.config = config
        
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
            logger.info(f"Config saved to {self.config_file}")
        except Exception as e:
            logger.error(f"Error saving config: {e}")
    
    def get_jobs(self):
        """Get list of enabled jobs"""
        if not self.config.get('enabled', True):
            return []
        
        return [job for job in self.config.get('jobs', []) if job.get('enabled', True)]
    
    def update_job(self, job_name, **kwargs):
        """Update job configuration"""
        for job in self.config.get('jobs', []):
            if job['name'] == job_name:
                job.update(kwargs)
                self.save_config()
                return True
        return False
    
    def add_job(self, name, time_str, command, enabled=True):
        """Add new scheduled job"""
        job = {
            "name": name,
            "time": time_str,
            "command": command,
            "enabled": enabled
        }
        self.config['jobs'].append(job)
        self.save_config()
    
    def remove_job(self, job_name):
        """Remove scheduled job"""
        self.config['jobs'] = [j for j in self.config['jobs'] if j['name'] != job_name]
        self.save_config()


class TradingScheduler:
    """Scheduler for automated trading tasks"""
    
    def __init__(self, config: SchedulerConfig):
        self.config = config
        self.running = False
        self.thread = None
        self.executed_today = set()  # Track executed jobs for today
    
    def start(self):
        """Start the scheduler"""
        if self.running:
            logger.warning("Scheduler already running")
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        logger.info("Scheduler started")
    
    def stop(self):
        """Stop the scheduler"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Scheduler stopped")
    
    def _run(self):
        """Main scheduler loop"""
        while self.running:
            try:
                current_time = datetime.now().time()
                current_date = datetime.now().date()
                
                # Reset executed jobs at midnight
                if not self.executed_today or datetime.now().date() != current_date:
                    self.executed_today.clear()
                
                # Check all jobs
                jobs = self.config.get_jobs()
                for job in jobs:
                    job_key = f"{job['name']}_{current_date}"
                    
                    # Skip if already executed today
                    if job_key in self.executed_today:
                        continue
                    
                    # Parse job time
                    try:
                        job_time = dt_time.fromisoformat(job['time'])
                    except:
                        logger.error(f"Invalid time format for job {job['name']}: {job['time']}")
                        continue
                    
                    # Check if it's time to execute (within 1 minute window)
                    if self._is_time_to_execute(current_time, job_time):
                        logger.info(f"Executing scheduled job: {job['name']}")
                        self._execute_job(job)
                        self.executed_today.add(job_key)
                
                # Sleep for 30 seconds
                time.sleep(30)
                
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                time.sleep(60)
    
    def _is_time_to_execute(self, current, target):
        """Check if current time matches target time (within 1 minute)"""
        current_seconds = current.hour * 3600 + current.minute * 60 + current.second
        target_seconds = target.hour * 3600 + target.minute * 60 + target.second
        
        # Within 1 minute window
        return 0 <= (current_seconds - target_seconds) < 60
    
    def _execute_job(self, job):
        """Execute a scheduled job"""
        try:
            # Change to SellerMarket directory
            cwd = Path(__file__).parent
            
            logger.info(f"Running: {job['command']}")
            
            # Execute command
            result = subprocess.run(
                job['command'],
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if result.returncode == 0:
                logger.info(f"Job {job['name']} completed successfully")
                logger.debug(f"Output: {result.stdout}")
            else:
                logger.error(f"Job {job['name']} failed with code {result.returncode}")
                logger.error(f"Error: {result.stderr}")
                
        except subprocess.TimeoutExpired:
            logger.error(f"Job {job['name']} timed out after 5 minutes")
        except Exception as e:
            logger.error(f"Error executing job {job['name']}: {e}")


class TradingBotService:
    """Main service that runs Telegram bot and scheduler"""
    
    def __init__(self):
        self.scheduler_config = SchedulerConfig()
        self.scheduler = TradingScheduler(self.scheduler_config)
        self.bot_process = None
        self.running = False
    
    def start(self):
        """Start the service"""
        logger.info("Starting Trading Bot Service")
        self.running = True
        
        # Start scheduler
        self.scheduler.start()
        
        # Start Telegram bot in subprocess
        self._start_bot()
        
        # Keep service running
        try:
            while self.running:
                # Check if bot process is still running
                if self.bot_process and self.bot_process.poll() is not None:
                    logger.warning("Bot process died, restarting...")
                    self._start_bot()
                
                time.sleep(10)
        except KeyboardInterrupt:
            logger.info("Received shutdown signal")
        finally:
            self.stop()
    
    def stop(self):
        """Stop the service"""
        logger.info("Stopping Trading Bot Service")
        self.running = False
        
        # Stop scheduler
        self.scheduler.stop()
        
        # Stop bot process
        if self.bot_process:
            self.bot_process.terminate()
            try:
                self.bot_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.bot_process.kill()
        
        logger.info("Service stopped")
    
    def _start_bot(self):
        """Start Telegram bot process"""
        try:
            bot_script = Path(__file__).parent / 'simple_config_bot.py'
            
            logger.info(f"Starting Telegram bot: {bot_script}")
            
            self.bot_process = subprocess.Popen(
                [sys.executable, str(bot_script)],
                cwd=Path(__file__).parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            logger.info(f"Bot started with PID: {self.bot_process.pid}")
            
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")


# Windows Service wrapper (if pywin32 available)
if HAS_PYWIN32:
    class TradingBotWindowsService(win32serviceutil.ServiceFramework):
        _svc_name_ = 'TradingBotService'
        _svc_display_name_ = 'Trading Bot Service'
        _svc_description_ = 'Automated trading bot with Telegram control and scheduler'
        
        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.service = TradingBotService()
        
        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)
            self.service.stop()
        
        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, '')
            )
            self.service.start()


def main():
    """Main entry point"""
    if HAS_PYWIN32 and len(sys.argv) > 1:
        # Running as Windows service
        win32serviceutil.HandleCommandLine(TradingBotWindowsService)
    else:
        # Running standalone
        logger.info("Starting Trading Bot Service (standalone mode)")
        logger.info("Press Ctrl+C to stop")
        
        service = TradingBotService()
        try:
            service.start()
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
            service.stop()


if __name__ == '__main__':
    main()
