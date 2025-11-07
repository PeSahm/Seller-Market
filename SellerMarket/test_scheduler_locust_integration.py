#!/usr/bin/env python3
"""
Test script to verify scheduler correctly loads Locust config
"""

from scheduler import JobScheduler, build_locust_command_from_config, load_locust_config
import json

def test_locust_config_loading():
    """Test that locust_config.json is loaded correctly"""
    print("="*80)
    print("Testing Locust Config Loading")
    print("="*80)
    
    config = load_locust_config()
    print("\nLoaded Locust Config:")
    print(json.dumps(config, indent=2))
    
    assert 'users' in config
    assert 'spawn_rate' in config
    assert 'run_time' in config
    assert 'host' in config
    print("\n✅ Locust config loaded successfully")

def test_command_building():
    """
    Verify that a base Locust CLI command is augmented with configured Locust parameters.
    
    Asserts that the resulting command includes the flags `--users`, `--spawn-rate`, `--run-time`, and `--host`.
    """
    print("\n" + "="*80)
    print("Testing Locust Command Building")
    print("="*80)
    
    base_command = "locust -f locustfile_new.py --headless"
    full_command = build_locust_command_from_config(base_command)
    
    print(f"\nBase command: {base_command}")
    print(f"Full command: {full_command}")
    
    # Verify that the command contains expected parameters
    assert '--users' in full_command
    assert '--spawn-rate' in full_command
    assert '--run-time' in full_command
    assert '--host' in full_command
    print("\n✅ Command built successfully")

def test_scheduler_integration():
    """Test that scheduler loads config and builds commands correctly"""
    print("\n" + "="*80)
    print("Testing Scheduler Integration")
    print("="*80)
    
    scheduler = JobScheduler('scheduler_config.json')
    config = scheduler.load_config()
    
    print(f"\nScheduler enabled: {config.get('enabled')}")
    print(f"Number of jobs: {len(config.get('jobs', []))}")
    
    # Find the run_trading job
    run_trading_job = None
    for job in config.get('jobs', []):
        if job.get('name') == 'run_trading':
            run_trading_job = job
            break
    
    assert run_trading_job is not None, "run_trading job not found"
    
    print(f"\nRun Trading Job:")
    print(f"  Name: {run_trading_job['name']}")
    print(f"  Time: {run_trading_job['time']}")
    print(f"  Base Command: {run_trading_job['command']}")
    
    # Build full command
    full_command = build_locust_command_from_config(run_trading_job['command'])
    print(f"  Full Command: {full_command}")
    
    # Verify the base command is simple (no hardcoded params)
    assert '--users' not in run_trading_job['command'], "Command should not have hardcoded --users"
    assert '--spawn-rate' not in run_trading_job['command'], "Command should not have hardcoded --spawn-rate"
    assert '--run-time' not in run_trading_job['command'], "Command should not have hardcoded --run-time"
    
    # Verify the full command has all params
    assert '--users' in full_command
    assert '--spawn-rate' in full_command
    assert '--run-time' in full_command
    assert '--host' in full_command
    
    print("\n✅ Scheduler integration working correctly")

def test_non_locust_commands():
    """Test that non-locust commands are not modified"""
    print("\n" + "="*80)
    print("Testing Non-Locust Commands")
    print("="*80)
    
    python_command = "python cache_warmup.py"
    result = build_locust_command_from_config(python_command)
    
    print(f"\nOriginal command: {python_command}")
    print(f"Processed command: {result}")
    
    assert result == python_command, "Non-locust commands should not be modified"
    print("\n✅ Non-locust commands preserved correctly")

if __name__ == '__main__':
    try:
        test_locust_config_loading()
        test_command_building()
        test_scheduler_integration()
        test_non_locust_commands()
        
        print("\n" + "="*80)
        print("✅ ALL TESTS PASSED")
        print("="*80)
        print("\nSummary:")
        print("- Locust config loads from locust_config.json")
        print("- Scheduler builds full Locust commands dynamically")
        print("- scheduler_config.json now uses simple base command")
        print("- All Locust parameters centralized in locust_config.json")
        print("- Non-locust commands remain unchanged")
        
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        exit(1)
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        exit(1)