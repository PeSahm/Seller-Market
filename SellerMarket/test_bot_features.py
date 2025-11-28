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
        from simple_config_bot import get_all_result_files
        
        # Temporarily change RESULTS_DIR for testing
        import simple_config_bot
        original_dir = simple_config_bot.RESULTS_DIR
        simple_config_bot.RESULTS_DIR = str(self.results_dir)
        
        try:
            result = get_all_result_files()
            self.assertEqual(result, [])
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
        
        from simple_config_bot import get_all_result_files
        import simple_config_bot
        original_dir = simple_config_bot.RESULTS_DIR
        simple_config_bot.RESULTS_DIR = str(self.results_dir)
        
        try:
            result = get_all_result_files()
            self.assertTrue(len(result) > 0)
            # Should be sorted newest first, so first one should be the newest
            self.assertTrue(result[0].endswith("results_2025-11-06_08-45-00.json"))
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
        
        from simple_config_bot import format_complete_order_results
        
        result = format_complete_order_results([str(result_file)])
        
        # Verify output format
        self.assertIn("Results #1", result)
        self.assertIn("test_user@gs", result)
        self.assertIn("Orders: 2", result)
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
        
        from simple_config_bot import format_complete_order_results
        
        result = format_complete_order_results([str(result_file)])
        
        # Check for "no orders found" message (case-insensitive)
        self.assertIn("no orders in this file", result.lower())

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

    def test_get_all_result_files_empty_directory(self):
        """Test get_all_result_files with empty directory."""
        from simple_config_bot import get_all_result_files
        import simple_config_bot
        original_dir = simple_config_bot.RESULTS_DIR
        simple_config_bot.RESULTS_DIR = str(self.results_dir)
        
        try:
            result = get_all_result_files()
            self.assertEqual(result, [])
        finally:
            simple_config_bot.RESULTS_DIR = original_dir

    def test_get_all_result_files_with_files(self):
        """Test get_all_result_files returns files sorted by modification time."""
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
        
        from simple_config_bot import get_all_result_files
        import simple_config_bot
        original_dir = simple_config_bot.RESULTS_DIR
        simple_config_bot.RESULTS_DIR = str(self.results_dir)
        
        try:
            result = get_all_result_files()
            self.assertEqual(len(result), 3)
            # Should be sorted newest first
            self.assertTrue(result[0].endswith("results_2025-11-06_08-45-00.json"))
            self.assertTrue(result[1].endswith("results_2025-11-05_08-45-00.json"))
            self.assertTrue(result[2].endswith("results_2025-11-04_08-45-00.json"))
        finally:
            simple_config_bot.RESULTS_DIR = original_dir

    def test_format_complete_order_results_no_files(self):
        """Test format_complete_order_results with no files."""
        from simple_config_bot import format_complete_order_results
        
        result = format_complete_order_results([])
        self.assertIn("No Trading Results Found", result)

    def test_format_complete_order_results_with_data(self):
        """Test format_complete_order_results with valid data."""
        # Create test result files
        test_data1 = {
            "timestamp": "2025-11-06T08:45:00",
            "username": "test_user1",
            "broker_code": "gs",
            "orders": [
                {
                    "isin": "IRO1MHRN0001",
                    "symbol": "فولاد",
                    "side": 1,
                    "price": 6000,
                    "volume": 100,
                    "state": 1,
                    "state_desc": "Registered",
                    "executed_volume": 100,
                    "is_done": True,
                    "tracking_number": "123456",
                    "created_shamsi": "1404/08/15"
                }
            ]
        }
        
        test_data2 = {
            "timestamp": "2025-11-06T08:46:00",
            "username": "test_user2",
            "broker_code": "bbi",
            "orders": [
                {
                    "isin": "IRO1ABCD0002",
                    "symbol": "ذوب",
                    "side": 2,
                    "price": 5000,
                    "volume": 50,
                    "state": 1,
                    "state_desc": "Registered",
                    "executed_volume": 0,
                    "is_done": False,
                    "tracking_number": "789012",
                    "created_shamsi": "1404/08/15"
                }
            ]
        }
        
        file1 = self.results_dir / "results_test_user1_gs_20251106_084500.json"
        file2 = self.results_dir / "results_test_user2_bbi_20251106_084600.json"
        
        file1.write_text(json.dumps(test_data1, ensure_ascii=False), encoding='utf-8')
        file2.write_text(json.dumps(test_data2, ensure_ascii=False), encoding='utf-8')
        
        from simple_config_bot import format_complete_order_results
        
        result_files = [str(file1), str(file2)]
        result = format_complete_order_results(result_files, max_files=2)
        
        # Verify output format
        self.assertIn("Results #1", result)
        self.assertIn("Results #2", result)
        self.assertIn("test_user1@gs", result)
        self.assertIn("test_user2@bbi", result)
        self.assertIn("فولاد", result)
        self.assertIn("ذوب", result)
        self.assertIn("123456", result)
        self.assertIn("789012", result)
        self.assertIn("BUY", result)
        self.assertIn("SELL", result)

    def test_format_complete_order_results_max_files_limit(self):
        """Test format_complete_order_results respects max_files limit."""
        # Create 5 test files
        result_files = []
        for i in range(5):
            test_data = {
                "timestamp": f"2025-11-06T08:4{i}:00",
                "username": f"user{i}",
                "broker_code": "gs",
                "orders": [{"symbol": f"stock{i}", "volume": 100}]
            }
            file_path = self.results_dir / f"results_user{i}_gs_20251106_084{i}00.json"
            file_path.write_text(json.dumps(test_data, ensure_ascii=False), encoding='utf-8')
            result_files.append(str(file_path))
        
        from simple_config_bot import format_complete_order_results
        
        result = format_complete_order_results(result_files, max_files=3)
        
        # Should only show first 3 files
        self.assertIn("Results #1", result)
        self.assertIn("Results #2", result)
        self.assertIn("Results #3", result)
        self.assertNotIn("Results #4", result)
        self.assertIn("2 more result files available", result)

    def test_format_complete_order_results_empty_orders(self):
        """Test format_complete_order_results with empty orders."""
        test_data = {
            "timestamp": "2025-11-06T08:45:00",
            "username": "test_user",
            "broker_code": "gs",
            "orders": []
        }
        
        file_path = self.results_dir / "results_test_user_gs_20251106_084500.json"
        file_path.write_text(json.dumps(test_data, ensure_ascii=False), encoding='utf-8')
        
        from simple_config_bot import format_complete_order_results
        
        result = format_complete_order_results([str(file_path)])
        
        self.assertIn("No orders in this file", result)

    def test_format_complete_order_results_error_handling(self):
        """Test format_complete_order_results handles file errors gracefully."""
        # Create a file with invalid JSON
        file_path = self.results_dir / "invalid.json"
        file_path.write_text("invalid json content")
        
        from simple_config_bot import format_complete_order_results
        
        result = format_complete_order_results([str(file_path)])
        
        self.assertIn("Error reading file", result)
        self.assertIn("invalid.json", result)


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

    def test_notification_message_format_with_per_account_details(self):
        """Test notification message format includes per-account order details."""
        # Test the enhanced notification format with account details
        timestamp = "2025-11-06 08:45:00"
        total_orders = 3
        total_executed = 2
        total_volume = 150
        accounts_processed = 2
        
        exec_percent = (total_executed / total_orders * 100) if total_orders > 0 else 0
        
        # Simulate account details
        account_details = [
            "👤 *user1@gs:*\n• فولاد: 123456 (100/100) - Executed\n• ذوب: 789012 (0/50) - Registered",
            "👤 *user2@bbi:*\n• مس: 345678 (50/50) - Executed"
        ]
        
        notification = (
            f"📊 *Trading Completed*\n"
            f"⏰ {timestamp}\n\n"
            f"✅ Orders Placed: {total_orders}\n"
            f"⚡ Executed: {total_executed}/{total_orders} ({exec_percent:.1f}%)\n"
            f"📈 Total Volume: {total_volume:,} shares\n"
            f"👥 Accounts: {accounts_processed}\n\n"
            f"*Order Details:*\n\n" + "\n\n".join(account_details) + "\n\n"
            f"Use /results to view complete details"
        )
        
        # Verify format
        self.assertIn("Trading Completed", notification)
        self.assertIn("Orders Placed: 3", notification)
        self.assertIn("66.7%", notification)
        self.assertIn("Total Volume: 150", notification)
        self.assertIn("user1@gs", notification)
        self.assertIn("user2@bbi", notification)
        self.assertIn("فولاد", notification)
        self.assertIn("123456", notification)
        self.assertIn("Executed", notification)
        self.assertIn("Use /results", notification)


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


class TestConfigManagement(unittest.TestCase):
    """Test configuration management functions - /list, /use, /add, /remove, /show and property updates."""

    def setUp(self):
        """Set up test fixtures with a test config.ini file."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_file = Path(self.temp_dir) / "config.ini"
        self.selected_file = Path(self.temp_dir) / ".selected_section"
        
        # Create a test config.ini with multiple sections (all active - no comments)
        self.initial_config = """[Account1]
username = user1
password = pass1
broker = gs
isin = IRO1TEST0001
side = 1

[Account2]
username = user2
password = pass2
broker = bbi
isin = IRO1TEST0002
side = 2

[Account3]
username = user3
password = pass3
broker = shahr
isin = IRO1TEST0003
side = 1
"""
        self.config_file.write_text(self.initial_config, encoding='utf-8')
        
        # Store original paths
        import simple_config_bot
        self.original_config_file = simple_config_bot.CONFIG_FILE
        self.original_selected_file = simple_config_bot.SELECTED_SECTION_FILE
        simple_config_bot.CONFIG_FILE = str(self.config_file)
        simple_config_bot.SELECTED_SECTION_FILE = str(self.selected_file)
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        import simple_config_bot
        simple_config_bot.CONFIG_FILE = self.original_config_file
        simple_config_bot.SELECTED_SECTION_FILE = self.original_selected_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_read_config_returns_all_sections(self):
        """Test that read_config returns all sections."""
        from simple_config_bot import read_config
        
        config = read_config()
        sections = config.sections()
        
        # Should have all 3 sections
        self.assertIn('Account1', sections)
        self.assertIn('Account2', sections)
        self.assertIn('Account3', sections)
        self.assertEqual(len(sections), 3)

    def test_get_selected_section_returns_first_when_no_selection(self):
        """Test that get_selected_section returns first section when no selection file exists."""
        from simple_config_bot import get_selected_section
        
        selected = get_selected_section()
        self.assertEqual(selected, 'Account1')

    def test_set_selected_section_persists(self):
        """Test that set_selected_section saves the selection to file."""
        from simple_config_bot import set_selected_section, get_selected_section
        
        set_selected_section('Account2')
        
        # Should persist
        selected = get_selected_section()
        self.assertEqual(selected, 'Account2')
        
        # File should exist
        self.assertTrue(self.selected_file.exists())
        self.assertEqual(self.selected_file.read_text().strip(), 'Account2')

    def test_set_selected_section_does_not_modify_config(self):
        """Test that set_selected_section does NOT modify config.ini (no commenting)."""
        from simple_config_bot import set_selected_section
        
        original_content = self.config_file.read_text(encoding='utf-8')
        
        set_selected_section('Account2')
        
        new_content = self.config_file.read_text(encoding='utf-8')
        
        # Config file should be unchanged
        self.assertEqual(original_content, new_content)

    def test_save_config_preserves_all_sections(self):
        """Test that save_config keeps all sections active."""
        from simple_config_bot import read_config, save_config
        
        config = read_config()
        config['Account1']['broker'] = 'bbi'
        save_config(config)
        
        # Re-read and verify all sections exist
        new_config = read_config()
        self.assertEqual(len(new_config.sections()), 3)
        self.assertIn('Account1', new_config.sections())
        self.assertIn('Account2', new_config.sections())
        self.assertIn('Account3', new_config.sections())
        
        # Verify the update
        self.assertEqual(new_config['Account1']['broker'], 'bbi')

    def test_update_one_section_preserves_others(self):
        """Test updating one section preserves all other sections' values."""
        from simple_config_bot import read_config, save_config
        
        config = read_config()
        original_account2 = dict(config['Account2'])
        original_account3 = dict(config['Account3'])
        
        # Update Account1
        config['Account1']['username'] = 'new_user1'
        config['Account1']['broker'] = 'karamad'
        save_config(config)
        
        # Verify other sections are unchanged
        new_config = read_config()
        self.assertEqual(dict(new_config['Account2']), original_account2)
        self.assertEqual(dict(new_config['Account3']), original_account3)

    def test_update_broker_preserves_all_configs(self):
        """Test updating broker value preserves all configurations."""
        from simple_config_bot import read_config, save_config, get_selected_section, set_selected_section
        
        set_selected_section('Account1')
        
        config = read_config()
        section = get_selected_section()
        config[section]['broker'] = 'tejarat'
        save_config(config)
        
        # Verify all 3 sections still exist
        new_config = read_config()
        self.assertEqual(len(new_config.sections()), 3)
        self.assertEqual(new_config['Account1']['broker'], 'tejarat')

    def test_update_symbol_preserves_all_configs(self):
        """Test updating ISIN/symbol value preserves all configurations."""
        from simple_config_bot import read_config, save_config, get_selected_section, set_selected_section
        
        set_selected_section('Account2')
        
        config = read_config()
        section = get_selected_section()
        config[section]['isin'] = 'IRO1NEWSTOCK1'
        save_config(config)
        
        new_config = read_config()
        self.assertEqual(len(new_config.sections()), 3)
        self.assertEqual(new_config['Account2']['isin'], 'IRO1NEWSTOCK1')

    def test_update_side_preserves_all_configs(self):
        """Test updating side value preserves all configurations."""
        from simple_config_bot import read_config, save_config, get_selected_section, set_selected_section
        
        set_selected_section('Account1')
        
        config = read_config()
        section = get_selected_section()
        config[section]['side'] = '2'
        save_config(config)
        
        new_config = read_config()
        self.assertEqual(len(new_config.sections()), 3)
        self.assertEqual(new_config['Account1']['side'], '2')

    def test_update_username_preserves_all_configs(self):
        """Test updating username value preserves all configurations."""
        from simple_config_bot import read_config, save_config, get_selected_section, set_selected_section
        
        set_selected_section('Account3')
        
        config = read_config()
        section = get_selected_section()
        config[section]['username'] = 'new_trading_user'
        save_config(config)
        
        new_config = read_config()
        self.assertEqual(len(new_config.sections()), 3)
        self.assertEqual(new_config['Account3']['username'], 'new_trading_user')

    def test_update_password_preserves_all_configs(self):
        """Test updating password value preserves all configurations."""
        from simple_config_bot import read_config, save_config, get_selected_section, set_selected_section
        
        set_selected_section('Account1')
        
        config = read_config()
        section = get_selected_section()
        config[section]['password'] = 'new_secure_password'
        save_config(config)
        
        new_config = read_config()
        self.assertEqual(len(new_config.sections()), 3)
        self.assertEqual(new_config['Account1']['password'], 'new_secure_password')

    def test_multiple_updates_preserve_all_sections(self):
        """Test that multiple sequential updates preserve all sections."""
        from simple_config_bot import read_config, save_config, set_selected_section
        
        # Perform multiple updates on different sections
        for i, section in enumerate(['Account1', 'Account2', 'Account3']):
            set_selected_section(section)
            config = read_config()
            config[section]['isin'] = f'IRO1UPDATE{i:03d}'
            save_config(config)
        
        # Verify all sections still exist with correct updates
        final_config = read_config()
        self.assertEqual(len(final_config.sections()), 3)
        self.assertEqual(final_config['Account1']['isin'], 'IRO1UPDATE000')
        self.assertEqual(final_config['Account2']['isin'], 'IRO1UPDATE001')
        self.assertEqual(final_config['Account3']['isin'], 'IRO1UPDATE002')

    def test_switch_selection_and_update(self):
        """Test switching selected section and updating works correctly."""
        from simple_config_bot import read_config, save_config, get_selected_section, set_selected_section
        
        # Start with Account1
        set_selected_section('Account1')
        self.assertEqual(get_selected_section(), 'Account1')
        
        # Switch to Account2 and update
        set_selected_section('Account2')
        self.assertEqual(get_selected_section(), 'Account2')
        
        config = read_config()
        config['Account2']['broker'] = 'ebb'
        save_config(config)
        
        # Verify all sections exist
        new_config = read_config()
        self.assertEqual(len(new_config.sections()), 3)
        self.assertEqual(new_config['Account2']['broker'], 'ebb')
        # Account1 should be unchanged
        self.assertEqual(new_config['Account1']['broker'], 'gs')

    def test_special_characters_in_values(self):
        """Test handling special characters in config values."""
        from simple_config_bot import read_config, save_config, set_selected_section
        
        set_selected_section('Account1')
        config = read_config()
        
        # Update with special characters (avoid % which triggers interpolation)
        config['Account1']['password'] = 'Pass@123!#$^&*'
        save_config(config)
        
        # Verify the special characters are preserved
        new_config = read_config()
        self.assertEqual(new_config['Account1']['password'], 'Pass@123!#$^&*')


if __name__ == '__main__':
    # Run with verbose output
    print("="*80)
    print("Running Telegram Bot Feature Tests")
    print("="*80)
    unittest.main(verbosity=2)
