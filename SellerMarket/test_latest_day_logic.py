#!/usr/bin/env python3
"""
Test script to verify the latest day order processing logic
"""
import json
from pathlib import Path
from datetime import datetime

def test_latest_day_logic():
    """Test the logic for finding latest date and processing orders"""

    # Simulate the logic from locustfile_new.py
    all_result_files = list(Path('order_results').glob('*.json'))
    print(f"Found {len(all_result_files)} result files")

    if all_result_files:
        # Extract dates from filenames
        file_dates = []
        for file_path in all_result_files:
            try:
                parts = file_path.stem.split('_')
                if len(parts) >= 3:
                    date_str = parts[2]  # YYYYMMDD
                    if len(date_str) == 8:
                        file_date = datetime.strptime(date_str, '%Y%m%d').date()
                        file_dates.append(file_date)
                        print(f"File: {file_path.name} -> Date: {file_date}")
            except (ValueError, IndexError) as e:
                print(f"Error parsing date from {file_path.name}: {e}")
                continue

        latest_date = max(file_dates) if file_dates else datetime.now().date()
        print(f"\nLatest date found: {latest_date}")

        # Test for account 0052707520@karamad
        username = '0052707520'
        broker_code = 'karamad'
        date_str = latest_date.strftime('%Y%m%d')
        result_files = [f for f in Path('order_results').glob(f'*{username}_{broker_code}_{date_str}_*.json')]

        print(f"\nFiles for {username}@{broker_code} on {latest_date}: {len(result_files)}")
        for f in result_files:
            print(f"  - {f.name}")

        if result_files:
            # Collect all orders
            all_orders = []
            for file_path in result_files:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        file_orders = data.get('orders', [])
                        all_orders.extend(file_orders)
                        print(f"  Read {len(file_orders)} orders from {file_path.name}")
                except Exception as e:
                    print(f"  Error reading {file_path.name}: {e}")

            print(f"\nTotal orders collected: {len(all_orders)}")

            # Group by symbol and find minimum tracking number
            symbol_orders = {}
            for order in all_orders:
                symbol = order.get('symbol', 'N/A')
                tracking_number = order.get('tracking_number', 0)

                if symbol not in symbol_orders or tracking_number < symbol_orders[symbol]['tracking_number']:
                    symbol_orders[symbol] = {
                        'symbol': symbol,
                        'tracking_number': tracking_number,
                        'created_shamsi': order.get('created_shamsi', 'N/A'),
                        'price': order.get('price', 0),
                        'volume': order.get('volume', 0),
                        'state_desc': order.get('state_desc', 'Unknown')
                    }

            print(f"\nSymbols found: {len(symbol_orders)}")
            for symbol, data in symbol_orders.items():
                print(f"  {symbol}: #{data['tracking_number']} | {data['created_shamsi']} | {data['price']:,} | {data['volume']:,}")

if __name__ == '__main__':
    test_latest_day_logic()