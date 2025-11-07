#!/usr/bin/env python3
"""
Test script to verify limit parameter validation in config_api.py
"""

import requests
import json
import subprocess
import time
import sys
import os

def test_limit_validation():
    """Test the limit parameter validation in the /results endpoint"""

    # Start the API server in background
    print("Starting API server...")
    server_process = subprocess.Popen([
        sys.executable, 'config_api.py'
    ], cwd=os.path.dirname(__file__),
       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Wait for server to start
    time.sleep(2)

    base_url = "http://127.0.0.1:5000"
    test_user_id = "test_user"

    try:
        # Test 1: Valid limit (should work)
        print("Test 1: Valid limit parameter")
        response = requests.get(f"{base_url}/results/{test_user_id}?limit=5")
        print(f"Status: {response.status_code}")
        if response.status_code == 200:
            print("✅ Valid limit accepted")
        else:
            print(f"❌ Expected 200, got {response.status_code}")

        # Test 2: Invalid limit - non-numeric (should return 400)
        print("\nTest 2: Invalid limit parameter - non-numeric")
        response = requests.get(f"{base_url}/results/{test_user_id}?limit=abc")
        print(f"Status: {response.status_code}")
        if response.status_code == 400:
            print("✅ Non-numeric limit rejected with 400")
            try:
                error_data = response.json()
                print(f"Error message: {error_data.get('message', 'No message')}")
            except:
                print(f"Response text: {response.text}")
        else:
            print(f"❌ Expected 400, got {response.status_code}")

        # Test 3: Invalid limit - negative number (should return 400)
        print("\nTest 3: Invalid limit parameter - negative number")
        response = requests.get(f"{base_url}/results/{test_user_id}?limit=-5")
        print(f"Status: {response.status_code}")
        if response.status_code == 400:
            print("✅ Negative limit rejected with 400")
            try:
                error_data = response.json()
                print(f"Error message: {error_data.get('message', 'No message')}")
            except:
                print(f"Response text: {response.text}")
        else:
            print(f"❌ Expected 400, got {response.status_code}")

        # Test 4: Invalid limit - zero (should return 400)
        print("\nTest 4: Invalid limit parameter - zero")
        response = requests.get(f"{base_url}/results/{test_user_id}?limit=0")
        print(f"Status: {response.status_code}")
        if response.status_code == 400:
            print("✅ Zero limit rejected with 400")
            try:
                error_data = response.json()
                print(f"Error message: {error_data.get('message', 'No message')}")
            except:
                print(f"Response text: {response.text}")
        else:
            print(f"❌ Expected 400, got {response.status_code}")

        # Test 5: Invalid limit - too large (should return 400)
        print("\nTest 5: Invalid limit parameter - too large")
        response = requests.get(f"{base_url}/results/{test_user_id}?limit=2000")
        print(f"Status: {response.status_code}")
        if response.status_code == 400:
            print("✅ Too large limit rejected with 400")
            try:
                error_data = response.json()
                print(f"Error message: {error_data.get('message', 'No message')}")
            except:
                print(f"Response text: {response.text}")
        else:
            print(f"❌ Expected 400, got {response.status_code}")

        # Test 6: Valid limit at maximum (should work)
        print("\nTest 6: Valid limit parameter - at maximum")
        response = requests.get(f"{base_url}/results/{test_user_id}?limit=1000")
        print(f"Status: {response.status_code}")
        if response.status_code == 200:
            print("✅ Maximum valid limit accepted")
        else:
            print(f"❌ Expected 200, got {response.status_code}")

    except requests.exceptions.ConnectionError:
        print("❌ Could not connect to API server. Make sure it's running.")
        return False
    finally:
        # Stop the server
        print("\nStopping API server...")
        server_process.terminate()
        server_process.wait()

    return True

if __name__ == "__main__":
    print("Testing limit parameter validation in config_api.py")
    print("=" * 50)
    test_limit_validation()
    print("\nTest completed!")