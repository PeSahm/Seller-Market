#!/usr/bin/env python3
"""
Test script to verify scheduler correctly loads Locust config
"""

from scheduler import JobScheduler, build_locust_command_from_config, load_locust_config
import json
import shlex

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
    assert 'processes' in config, "processes parameter should be in config"
    print("\n✅ Locust config loaded successfully")

def test_command_building():
    """
    Verify that a base Locust CLI command is augmented with configured Locust parameters.
    
    Asserts that the resulting command includes the flags `--users`, `--spawn-rate`, `--run-time`, `--host`, and `--processes`.
    """
    print("\n" + "="*80)
    print("Testing Locust Command Building")
    print("="*80)
    
    base_command = "locust -f locustfile_new.py --headless"
    full_command_args = build_locust_command_from_config(base_command)
    
    print(f"\nBase command: {base_command}")
    print(f"Full command args: {full_command_args}")
    print(f"Full command string: {shlex.join(full_command_args)}")
    
    # Verify that the command args contain expected parameters
    assert '--users' in full_command_args
    assert '--spawn-rate' in full_command_args
    assert '--run-time' in full_command_args
    assert '--host' in full_command_args
    assert '--processes' in full_command_args, "--processes should be in command"
    
    # Verify that each parameter is a separate element
    users_index = full_command_args.index('--users')
    assert users_index + 1 < len(full_command_args)
    assert full_command_args[users_index + 1] == '10'  # Should be string
    
    spawn_rate_index = full_command_args.index('--spawn-rate')
    assert spawn_rate_index + 1 < len(full_command_args)
    assert full_command_args[spawn_rate_index + 1] == '10'
    
    run_time_index = full_command_args.index('--run-time')
    assert run_time_index + 1 < len(full_command_args)
    assert full_command_args[run_time_index + 1] == '30s'
    
    host_index = full_command_args.index('--host')
    assert host_index + 1 < len(full_command_args)
    assert full_command_args[host_index + 1] == 'https://abc.com'
    
    # Verify --processes parameter
    processes_index = full_command_args.index('--processes')
    assert processes_index + 1 < len(full_command_args)
    assert full_command_args[processes_index + 1] == '4', "processes should be '4'"
    
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
    full_command_args = build_locust_command_from_config(run_trading_job['command'])
    print(f"  Full Command Args: {full_command_args}")
    print(f"  Full Command String: {shlex.join(full_command_args)}")
    
    # Verify the base command is simple (no hardcoded params)
    assert '--users' not in run_trading_job['command'], "Command should not have hardcoded --users"
    assert '--spawn-rate' not in run_trading_job['command'], "Command should not have hardcoded --spawn-rate"
    assert '--run-time' not in run_trading_job['command'], "Command should not have hardcoded --run-time"
    
    # Verify the full command has all params
    assert '--users' in full_command_args
    assert '--spawn-rate' in full_command_args
    assert '--run-time' in full_command_args
    assert '--host' in full_command_args
    assert '--processes' in full_command_args, "--processes should be in full command"
    
    print("\n✅ Scheduler integration working correctly")

def test_non_locust_commands():
    """Test that non-locust commands are not modified"""
    print("\n" + "="*80)
    print("Testing Non-Locust Commands")
    print("="*80)
    
    python_command = "python cache_warmup.py"
    result_args = build_locust_command_from_config(python_command)
    
    print(f"\nOriginal command: {python_command}")
    print(f"Processed command args: {result_args}")
    print(f"Processed command string: {shlex.join(result_args)}")
    
    # For non-locust commands, should return the parsed command as list
    expected_args = shlex.split(python_command)
    assert result_args == expected_args, f"Expected {expected_args}, got {result_args}"
    print("\n✅ Non-locust commands preserved correctly")


def test_distributed_processes_config():
    """
    Test that --processes parameter is correctly loaded and applied for distributed load generation.
    
    The --processes flag enables running multiple Locust worker processes on a single machine,
    which is useful for better CPU utilization. Values can be:
    - A positive integer (e.g., 4) to spawn that many workers
    - -1 to auto-detect the number of CPU cores
    
    Note: This feature requires Linux/macOS as it uses fork().
    """
    print("\n" + "="*80)
    print("Testing Distributed Processes Configuration")
    print("="*80)
    
    config = load_locust_config()
    
    print(f"\nProcesses config value: {config.get('processes')}")
    
    # Verify processes is in config
    assert 'processes' in config, "processes should be defined in locust_config.json"
    
    # Build command and verify --processes is included
    base_command = "locust -f locustfile_new.py --headless"
    full_command_args = build_locust_command_from_config(base_command)
    
    print(f"Full command: {shlex.join(full_command_args)}")
    
    assert '--processes' in full_command_args, "--processes flag should be in command"
    
    processes_index = full_command_args.index('--processes')
    processes_value = full_command_args[processes_index + 1]
    
    print(f"--processes value: {processes_value}")
    
    # Value should be a valid integer string (positive or -1 for auto-detect)
    assert processes_value.lstrip('-').isdigit(), f"processes value '{processes_value}' should be an integer"
    
    print("\n✅ Distributed processes configuration working correctly")

if __name__ == '__main__':
    try:
        test_locust_config_loading()
        test_command_building()
        test_scheduler_integration()
        test_non_locust_commands()
        test_distributed_processes_config()
        
        print("\n" + "="*80)
        print("✅ ALL TESTS PASSED")
        print("="*80)
        print("\nSummary:")
        print("- Locust config loads from locust_config.json")
        print("- Scheduler builds full Locust commands dynamically")
        print("- scheduler_config.json now uses simple base command")
        print("- All Locust parameters centralized in locust_config.json")
        print("- Non-locust commands remain unchanged")
        print("- Distributed load generation via --processes supported")
        
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        exit(1)
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        exit(1)