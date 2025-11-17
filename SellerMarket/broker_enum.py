"""
Broker enumeration for Iranian stock exchanges.
Each broker has a unique code used in API endpoints.
"""

from enum import Enum


class BrokerCode(Enum):
    """Broker codes for ephoenix.ir platform endpoints."""
    
    GANJINE = "gs"  # Ghadir Shahr
    SHAHR = "shahr"  # Shahr
    BOURSE_BIME = "bbi"  # Bourse Bazar Iran
    KARAMAD = "karamad"  # Karamad
    TEJARAT = "tejarat"  # Tejarat
    EBB = "ebb"  # Eghtesad Bidar
    IBTRADER = "ib"  # IbTrader
    
    @classmethod
    def get_broker_name(cls, code: str) -> str:
        """Get broker name from code."""
        names = {
            "gs": "Ghadir Shahr (Ganjine)",
            "shahr": "Shahr",
            "bbi": "Bourse Bazar Iran",
            "karamad": "Karamad",
            "tejarat": "Tejarat",
            "ebb": "EBB",
            "ib": "IbTrader"
        }
        return names.get(code, code)
    
    @classmethod
    def is_valid(cls, code: str) -> bool:
        """Check if broker code is valid."""
        return code in [b.value for b in cls]
    
    def get_endpoints(self) -> dict:
        """Get API endpoints for this broker."""
        domain = "ibtrader.ir" if self.value == "ib" else "ephoenix.ir"
        prefix = "." if self.value == "ib" else f"-{self.value}."
        mdapi = "mdapi" if self.value == "ib" else "mdapi1"
        return {
            'captcha': f'https://identity{prefix}{domain}/api/Captcha/GetCaptcha',
            'login': f'https://identity{prefix}{domain}/api/v2/accounts/login',
            'order': f'https://api{prefix}{domain}/api/v2/orders/NewOrder',
            'editorder': f'https://api{prefix}{domain}/api/v2/orders/EditOrder',
            'trading_book': f'https://api{prefix}{domain}/api/v2/tradingbook/GetLastTradingBook',
            'calculate_order': f'https://api{prefix}{domain}/api/v2/orders/CalculateOrderParam',
            'open_orders': f'https://api{prefix}{domain}/api/v2/orders/GetOpenOrders',
            'market_data': f'https://{mdapi}.{domain}/api/v2/instruments/full'
        }
