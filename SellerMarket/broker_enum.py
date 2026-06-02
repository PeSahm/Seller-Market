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
    HAMERZ = "hbc"  # Hamerz
    RABIN = "rabin"  # Rabin
    AYANDEH = "ayandeh"
    FARABI = "farabi"  # Farabi (ephoenix.ir family)
    
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
            "ib": "IbTrader",
            "hbc": "Hamerz",
            "rabin": "Rabin",
            "ayandeh": "Ayandeh",
            "farabi": "Farabi"
        }
        return names.get(code, code)
    
    @classmethod
    def is_valid(cls, code: str) -> bool:
        """Check if broker code is valid."""
        return code in [b.value for b in cls]

    @classmethod
    def family(cls, code: str) -> str:
        """Fallback broker family classifier.

        The bot trusts the config-rendered ``broker_family`` (the mgmt UI emits
        it per stack), so this is only consulted when that key is absent. Every
        member of this enum is an ephoenix-family broker, so default to that.
        Exir/Rayan-HamAfza brokers are not enumerated here; they arrive purely
        via ``config_section['broker_family'] == 'exir'``.
        """
        return "ephoenix"
    
    def get_endpoints(self) -> dict:
        """Get API endpoints for this broker.

        Delegates to the module-level :func:`get_endpoints_for` so enumerated
        brokers and data-driven callers (a new ephoenix broker added purely via
        the mgmt UI's ``brokers`` table) produce byte-for-byte identical URLs.
        """
        return get_endpoints_for(self.value)


def get_endpoints_for(code: str) -> dict:
    """Derive the ephoenix-family API endpoints for any broker ``code``.

    The URL shape is uniform across the ephoenix family
    (``{service}-{code}.ephoenix.ir``); ``ib`` (IbTrader) is the one structural
    exception — its own ``ibtrader.ir`` domain plus an ``api8`` portfolio shard.
    Because every endpoint derives purely from the code string, a NEW *standard*
    ephoenix broker needs no enum entry: the mgmt UI's ``brokers`` row plus the
    rendered ``broker`` / ``broker_family = ephoenix`` are enough for the bot to
    trade it. This is the exact logic the enumerated brokers have always used
    (moved here verbatim from ``BrokerCode.get_endpoints`` so they stay
    byte-for-byte identical).
    """
    domain = "ibtrader.ir" if code == "ib" else "ephoenix.ir"
    prefix = "." if code == "ib" else f"-{code}."
    mdapi = "mdapi" if code == "ib" else "mdapi1"

    # Portfolio lives on a different host family than the regular api endpoints.
    # ephoenix family: backofficeexternal-{broker}.ephoenix.ir (verified on ayandeh;
    # pattern is assumed identical for the other brokers — confirm per-broker).
    # ib: api8.ibtrader.ir — a separate shard from the regular api.ibtrader.ir.
    if code == "ib":
        portfolio = 'https://api8.ibtrader.ir/api/portfolio/getrealsecuritypositionbydate'
        customer_info = 'https://api8.ibtrader.ir/api/party/getcustomerinfo'
    else:
        portfolio = (
            f'https://backofficeexternal{prefix}{domain}'
            '/api/portfolio/getrealsecuritypositionbydate'
        )
        customer_info = (
            f'https://backofficeexternal{prefix}{domain}'
            '/api/party/getcustomerinfo'
        )

    return {
        'captcha': f'https://identity{prefix}{domain}/api/Captcha/GetCaptcha',
        'login': f'https://identity{prefix}{domain}/api/v2/accounts/login',
        'order': f'https://api{prefix}{domain}/api/v2/orders/NewOrder',
        'editorder': f'https://api{prefix}{domain}/api/v2/orders/EditOrder',
        'trading_book': f'https://api{prefix}{domain}/api/v2/tradingbook/GetLastTradingBook',
        'calculate_order': f'https://api{prefix}{domain}/api/v2/orders/CalculateOrderParam',
        'open_orders': f'https://api{prefix}{domain}/api/v2/orders/GetOpenOrders',
        'market_data': f'https://{mdapi}.{domain}/api/v2/instruments/full',
        'portfolio': portfolio,
        'customer_info': customer_info,
    }
