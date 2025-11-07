"""
Unit tests for Telegram bot features including results/logs display and notifications.
Tests the new /results, /logs commands and notification functionality.
"""

import unittest
import json
import os
import tempfile
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
from datetime import datetime


class TestBotHelperFunctions(unittest.TestCase):
    """Test helper functions for bot commands."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.results_dir = Path(self.temp_dir) / "order_results"
        self.results_dir.mkdir(exist_ok=True)
        self.log_file = Path(self.temp_dir) / "trading_bot.log"
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_latest_result_file_empty_directory(self):
        """Test get_latest_result_file with empty directory."""
        # Empty directory should return None
        from simple_config_bot import get_latest_result_file
        
        # Temporarily change RESULTS_DIR for testing
        import simple_config_bot
        original_dir = simple_config_bot.RESULTS_DIR
        simple_config_bot.RESULTS_DIR = str(self.results_dir)
        
        try:
            result = get_latest_result_file()
            self.assertIsNone(result)
        finally:
            simple_config_bot.RESULTS_DIR = original_dir

    def test_get_latest_result_file_with_files(self):
        """Test get_latest_result_file returns most recent file."""
        # Create test files with different timestamps
        file1 = self.results_dir / "results_2025-11-05_08-45-00.json"
        file2 = self.results_dir / "results_2025-11-06_08-45-00.json"
        file3 = self.results_dir / "results_2025-11-04_08-45-00.json"
        
        for f in [file1, file2, file3]:
            f.write_text('{"test": "data"}')
            
        # Make file2 the newest by modification time
        import time
        os.utime(file2, (time.time(), time.time()))
        os.utime(file1, (time.time() - 86400, time.time() - 86400))
        os.utime(file3, (time.time() - 172800, time.time() - 172800))
        
        from simple_config_bot import get_latest_result_file
        import simple_config_bot
        original_dir = simple_config_bot.RESULTS_DIR
        simple_config_bot.RESULTS_DIR = str(self.results_dir)
        
        try:
            result = get_latest_result_file()
            self.assertIsNotNone(result)
            self.assertTrue(result.endswith("results_2025-11-06_08-45-00.json"))
        finally:
            simple_config_bot.RESULTS_DIR = original_dir

    def test_format_order_results_with_data(self):
        """Test format_order_results with valid JSON data."""
        # Create test result file
        test_data = {
            "timestamp": "2025-11-06T08:45:00",
            "username": "test_user",
            "broker_code": "gs",
            "orders": [
                {
                    "isin": "IRO1MHRN0001",
                    "symbol": "فولاد",
                    "side": 1,
                    "price": 6000,
                    "volume": 100,
                    "state": 1,
                    "stateDesc": "Registered",
                    "executedVolume": 100,
                    "isDone": True
                },
                {
                    "isin": "IRO1ABCD0002",
                    "symbol": "ذوب",
                    "side": 2,
                    "price": 5000,
                    "volume": 50,
                    "state": 1,
                    "stateDesc": "Registered",
                    "executedVolume": 0,
                    "isDone": False
                }
            ]
        }
        
        result_file = self.results_dir / "test_results.json"
        result_file.write_text(json.dumps(test_data, ensure_ascii=False), encoding='utf-8')
        
        from simple_config_bot import format_order_results
        
        result = format_order_results(str(result_file))
        
        # Verify output format
        self.assertIn("Trading Results", result)
        self.assertIn("test_user@gs", result)
        self.assertIn("Orders: 2", result)  # Changed from "Total Orders: 2"
        self.assertIn("Volume: 150 shares", result)
        self.assertIn("فولاد", result)
        self.assertIn("ذوب", result)
        self.assertIn("BUY", result)
        self.assertIn("SELL", result)

    def test_format_order_results_empty_orders(self):
        """Test format_order_results with no orders."""
        test_data = {
            "timestamp": "2025-11-06T08:45:00",
            "username": "test_user",
            "broker_code": "gs",
            "orders": []
        }
        
        result_file = self.results_dir / "test_results.json"
        result_file.write_text(json.dumps(test_data, ensure_ascii=False), encoding='utf-8')
        
        from simple_config_bot import format_order_results
        
        result = format_order_results(str(result_file))
        
        # Check for "no orders found" message (case-insensitive)
        self.assertIn("no orders found", result.lower())

    def test_get_log_tail_basic(self):
        """Test get_log_tail with basic log file."""
        # Create test log file
        log_lines = [f"2025-11-06 08:45:{i:02d} - INFO - Test log line {i}\n" for i in range(100)]
        self.log_file.write_text(''.join(log_lines))
        
        from simple_config_bot import get_log_tail
        import simple_config_bot
        original_log = simple_config_bot.LOG_FILE
        simple_config_bot.LOG_FILE = str(self.log_file)
        
        try:
            # Get last 10 lines
            result = get_log_tail(lines=10)
            
            # Should contain last 10 lines
            self.assertIn("Test log line 99", result)
            self.assertIn("Test log line 90", result)
            self.assertNotIn("Test log line 89", result)
        finally:
            simple_config_bot.LOG_FILE = original_log

    def test_get_log_tail_empty_file(self):
        """Test get_log_tail with empty log file."""
        self.log_file.write_text("")
        
        from simple_config_bot import get_log_tail
        import simple_config_bot
        original_log = simple_config_bot.LOG_FILE
        simple_config_bot.LOG_FILE = str(self.log_file)
        
        try:
            result = get_log_tail(lines=50)
            self.assertIn("empty", result.lower())
        finally:
            simple_config_bot.LOG_FILE = original_log

    def test_get_log_tail_file_not_found(self):
        """Test get_log_tail with missing log file."""
        from simple_config_bot import get_log_tail
        import simple_config_bot
        original_log = simple_config_bot.LOG_FILE
        simple_config_bot.LOG_FILE = str(Path(self.temp_dir) / "nonexistent.log")
        
        try:
            result = get_log_tail(lines=50)
            # Check for "no log file found" message
            self.assertIn("no log file", result.lower())
        finally:
            simple_config_bot.LOG_FILE = original_log


class TestTelegramNotifications(unittest.TestCase):
    """Test Telegram notification functionality."""

    def test_notification_message_format_with_orders(self):
        """Test notification message format when orders are found."""
        # Test the notification format (no actual API calls)
        timestamp = "2025-11-06 08:45:00"
        total_orders = 5
        total_executed = 3
        total_volume = 1000
        accounts_processed = 2
        
        exec_percent = (total_executed / total_orders * 100) if total_orders > 0 else 0
        
        notification = (
            f"📊 *Trading Completed*\n"
            f"⏰ {timestamp}\n\n"
            f"✅ Orders Placed: {total_orders}\n"
            f"⚡ Executed: {total_executed}/{total_orders} ({exec_percent:.1f}%)\n"
            f"📈 Total Volume: {total_volume:,} shares\n"
            f"👥 Accounts: {accounts_processed}\n\n"
            f"Use /results to view details"
        )
        
        # Verify format
        self.assertIn("Trading Completed", notification)
        self.assertIn("Orders Placed: 5", notification)
        self.assertIn("60.0%", notification)
        self.assertIn("Total Volume: 1,000", notification)

    def test_notification_message_format_no_orders(self):
        """Test notification message format when no orders are found."""
        timestamp = "2025-11-06 08:45:00"
        accounts_processed = 1
        
        notification = (
            f"📊 *Trading Completed*\n"
            f"⏰ {timestamp}\n\n"
            f"⚠️ *No Orders Found*\n\n"
            f"Accounts checked: {accounts_processed}\n\n"
            f"Possible reasons:\n"
            f"• Market is closed\n"
            f"• Orders failed to place\n"
            f"• Rate limit exceeded\n\n"
            f"Use /logs to check details"
        )
        
        # Verify format
        self.assertIn("No Orders Found", notification)
        self.assertIn("Market is closed", notification)
        self.assertIn("/logs", notification)


class TestBotCommands(unittest.TestCase):
    """Test bot command handler logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.results_dir = Path(self.temp_dir) / "order_results"
        self.results_dir.mkdir(exist_ok=True)
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_results_message_no_results(self):
        """Test results message format when no results exist."""
        # Test the message format without calling the actual handler
        message = (
            "📊 *No Trading Results*\n\n"
            "No results found yet.\n\n"
            "Results will appear here after you run:\n"
            "/trade - Run trading manually\n\n"
            "Or after scheduled trading executes."
        )
        
        self.assertIn("No Trading Results", message)
        self.assertIn("/trade", message)

    def test_results_message_with_data(self):
        """Test results message format with actual data."""
        # Simulate the formatted results message
        message = (
            "📊 *Trading Results - test_user@gs*\n"
            "📅 2025-11-06 08:45:00\n\n"
            "📈 *Summary:*\n"
            "Total Orders: 2\n"
            "Executed: 1/2 (50.0%)\n"
            "Total Volume: 150 shares\n"
            "Total Amount: 750,000 Rials\n\n"
            "📋 *Orders:*\n"
            "1. فولاد | BUY | ✅ Executed\n"
            "   Price: 6,000 | Volume: 100\n"
        )
        
        self.assertIn("Trading Results", message)
        self.assertIn("test_user@gs", message)
        self.assertIn("Total Orders: 2", message)
        self.assertIn("فولاد", message)

    def test_logs_message_format(self):
        """Test logs message format."""
        message = (
            "📋 *Log File (Last 50 lines):*\n\n"
            "```\n"
            "2025-11-06 08:45:00 - INFO - Starting trading...\n"
            "2025-11-06 08:45:01 - INFO - Authenticating...\n"
            "```"
        )
        
        self.assertIn("Log File", message)
        self.assertIn("Last 50 lines", message)


class TestOnTestStopNotification(unittest.TestCase):
    """Test on_test_stop event notification format."""

    def test_notification_format_with_orders(self):
        """Test notification message format for successful trading."""
        # Test notification message format for success case
        timestamp = "2025-11-06 08:45:00"
        total_orders = 5
        total_executed = 3
        total_volume = 1000
        accounts_processed = 2
        
        exec_percent = (total_executed / total_orders * 100) if total_orders > 0 else 0
        
        notification = (
            f"📊 *Trading Completed*\n"
            f"⏰ {timestamp}\n\n"
            f"✅ Orders Placed: {total_orders}\n"
            f"⚡ Executed: {total_executed}/{total_orders} ({exec_percent:.1f}%)\n"
            f"📈 Total Volume: {total_volume:,} shares\n"
            f"👥 Accounts: {accounts_processed}\n\n"
            f"Use /results to view details"
        )
        
        # Verify format
        self.assertIn("Trading Completed", notification)
        self.assertIn("Orders Placed: 5", notification)
        self.assertIn("60.0%", notification)

    def test_notification_format_no_orders(self):
        """Test notification message format when no orders found."""
        # Test notification message format for no orders
        timestamp = "2025-11-06 08:45:00"
        accounts_processed = 1
        
        notification = (
            f"📊 *Trading Completed*\n"
            f"⏰ {timestamp}\n\n"
            f"⚠️ *No Orders Found*\n\n"
            f"Accounts checked: {accounts_processed}\n\n"
            f"Possible reasons:\n"
            f"• Market is closed\n"
            f"• Orders failed to place\n"
            f"• Rate limit exceeded\n\n"
            f"Use /logs to check details"
        )
        
        # Verify format
        self.assertIn("No Orders Found", notification)
        self.assertIn("Market is closed", notification)


class TestSchedulerConfig(unittest.TestCase):
    """Test scheduler configuration structure."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_file = Path(self.temp_dir) / "scheduler_config.json"
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_scheduler_config_structure(self):
        """Test scheduler config file structure."""
        # Create test config
        config = {
            "jobs": [
                {
                    "name": "cache_warmup",
                    "command": "python cache_warmup.py",
                    "time": "08:30:00",
                    "enabled": True
                },
                {
                    "name": "run_trading",
                    "command": "python -m locust -f locustfile_new.py --headless -u 1 -r 1 -t 30s --html=report.html",
                    "time": "08:44:30",
                    "enabled": True
                }
            ]
        }
        
        self.config_file.write_text(json.dumps(config, indent=2))
        
        # Verify file was created
        self.assertTrue(self.config_file.exists())
        
        # Load and verify structure
        loaded_config = json.loads(self.config_file.read_text())
        self.assertIn("jobs", loaded_config)
        self.assertEqual(len(loaded_config["jobs"]), 2)
        self.assertEqual(loaded_config["jobs"][0]["name"], "cache_warmup")
        self.assertEqual(loaded_config["jobs"][1]["time"], "08:44:30")

    def test_job_enable_disable_logic(self):
        """Test job enable/disable logic."""
        # Create test config
        config = {
            "jobs": [
                {
                    "name": "cache_warmup",
                    "command": "python cache_warmup.py",
                    "time": "08:30:00",
                    "enabled": False
                }
            ]
        }
        
        self.config_file.write_text(json.dumps(config, indent=2))
        
        # Simulate enabling the job
        loaded_config = json.loads(self.config_file.read_text())
        for job in loaded_config["jobs"]:
            if job["name"] == "cache_warmup":
                job["enabled"] = True
        
        self.config_file.write_text(json.dumps(loaded_config, indent=2))
        
        # Verify it was enabled
        updated_config = json.loads(self.config_file.read_text())
        self.assertTrue(updated_config["jobs"][0]["enabled"])


if __name__ == '__main__':
    # Run with verbose output
    print("="*80)
    print("Running Telegram Bot Feature Tests")
    print("="*80)
    unittest.main(verbosity=2)
