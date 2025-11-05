"""
Order management and result tracking.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


class OrderResult:
    """Represents an order execution result."""
    
    def __init__(self, order_data: dict):
        """Initialize from order API response."""
        self.isin = order_data.get('isin', '')
        self.symbol = order_data.get('symbol', '')
        self.symbol_title = order_data.get('symbolTitle', '')
        self.tracking_number = order_data.get('trackingNumber', 0)
        self.serial_number = order_data.get('serialNumber', 0)
        self.created = order_data.get('created', '')
        self.created_shamsi = order_data.get('createdShamsiDate', '')
        self.side = order_data.get('orderSide', 0)
        self.side_desc = 'Buy' if self.side == 1 else 'Sell'
        self.price = order_data.get('price', 0)
        self.volume = order_data.get('volume', 0)
        self.remained_volume = order_data.get('remainedVolume', 0)
        self.executed_volume = order_data.get('executedVolume', 0)
        self.state = order_data.get('state', 0)
        self.state_desc = order_data.get('stateDesc', '')
        self.is_done = order_data.get('isDone', False)
        self.net_amount = order_data.get('netAmount', 0)
        
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            'isin': self.isin,
            'symbol': self.symbol,
            'symbol_title': self.symbol_title,
            'tracking_number': self.tracking_number,
            'serial_number': self.serial_number,
            'created': self.created,
            'created_shamsi': self.created_shamsi,
            'side': self.side,
            'side_desc': self.side_desc,
            'price': self.price,
            'volume': self.volume,
            'remained_volume': self.remained_volume,
            'executed_volume': self.executed_volume,
            'state': self.state,
            'state_desc': self.state_desc,
            'is_done': self.is_done,
            'net_amount': self.net_amount
        }
    
    def __str__(self) -> str:
        """String representation."""
        return (f"Order {self.tracking_number}: {self.side_desc} {self.volume:,} x "
                f"{self.symbol} @ {self.price:,} - {self.state_desc}")


class OrderResultTracker:
    """Tracks and saves order results."""
    
    def __init__(self, results_dir: str = "order_results"):
        """
        Initialize tracker.
        
        Args:
            results_dir: Directory to save results
        """
        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(exist_ok=True)
        logger.info(f"Order results will be saved to {self.results_dir}")
    
    def save_order_results(self, username: str, broker_code: str, 
                          orders: List[OrderResult]):
        """
        Save order results to file.
        
        Args:
            username: Trading account username
            broker_code: Broker code
            orders: List of OrderResult objects
        """
        if not orders:
            logger.info(f"No orders to save for {username}@{broker_code}")
            return
        
        # Create filename with date
        date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = self.results_dir / f"{username}_{broker_code}_{date_str}.json"
        
        # Convert to list of dicts
        data = {
            'username': username,
            'broker_code': broker_code,
            'timestamp': datetime.now().isoformat(),
            'order_count': len(orders),
            'orders': [order.to_dict() for order in orders]
        }
        
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Saved {len(orders)} order results to {filename}")
            
            # Log summary
            for order in orders:
                logger.info(f"  {order}")
                
        except Exception as e:
            logger.error(f"Failed to save order results: {e}")
    
    def load_latest_results(self, username: str, broker_code: str) -> List[OrderResult]:
        """
        Load latest order results for a user.
        
        Args:
            username: Trading account username
            broker_code: Broker code
            
        Returns:
            List of OrderResult objects
        """
        pattern = f"{username}_{broker_code}_*.json"
        files = sorted(self.results_dir.glob(pattern), reverse=True)
        
        if not files:
            logger.info(f"No previous results found for {username}@{broker_code}")
            return []
        
        try:
            with open(files[0], 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            orders = [OrderResult(order_data) for order_data in data['orders']]
            logger.info(f"Loaded {len(orders)} orders from {files[0]}")
            
            return orders
            
        except Exception as e:
            logger.error(f"Failed to load order results: {e}")
            return []
    
    def get_summary_report(self, username: str, broker_code: str) -> str:
        """
        Generate summary report for latest orders.
        
        Args:
            username: Trading account username
            broker_code: Broker code
            
        Returns:
            Formatted summary string
        """
        orders = self.load_latest_results(username, broker_code)
        
        if not orders:
            return f"No orders found for {username}@{broker_code}"
        
        total_volume = sum(o.volume for o in orders)
        total_executed = sum(o.executed_volume for o in orders)
        total_amount = sum(o.net_amount for o in orders)
        
        report = [
            f"\n{'='*70}",
            f"Order Summary: {username}@{broker_code}",
            f"{'='*70}",
            f"Total Orders: {len(orders)}",
            f"Total Volume: {total_volume:,} shares",
            f"Executed Volume: {total_executed:,} shares ({total_executed/total_volume*100:.1f}%)" if total_volume > 0 else "Executed Volume: 0",
            f"Total Amount: {total_amount:,.0f} Rials",
            f"\nOrder Details:",
            f"{'-'*70}"
        ]
        
        for order in orders:
            report.append(
                f"{order.created_shamsi} | {order.symbol:8s} | "
                f"{order.side_desc:4s} | {order.volume:8,} @ {order.price:6,} | "
                f"{order.state_desc}"
            )
        
        report.append(f"{'='*70}\n")
        
        return '\n'.join(report)
