"""
Broker enumeration for Iranian stock exchanges.
Each broker has a unique code used in API endpoints.
"""

from enum import Enum

import runtime_config


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
    # Domain / market-data host / ib portfolio shard come from the DB-pushed
    # ``[runtime]`` section (fallback = the historical literal), so they can be
    # redirected fleet-wide with NO image rebuild (the mdapi1 -> marketdatagw
    # class of incident). No section ⇒ every read misses ⇒ identical to before.
    is_ib = code == "ib"
    if is_ib:
        domain = runtime_config.get("ib_domain", "ibtrader.ir")
        mdapi = runtime_config.get("ib_md_host", "mdapi")
    else:
        domain = runtime_config.get("ephoenix_domain", "ephoenix.ir")
        mdapi = runtime_config.get("ephoenix_md_host", "marketdatagw")
    prefix = "." if is_ib else f"-{code}."

    # Portfolio lives on a different host family than the regular api endpoints.
    # ephoenix family: backofficeexternal-{broker}.ephoenix.ir (verified on ayandeh;
    # pattern is assumed identical for the other brokers — confirm per-broker).
    # ib: api8.ibtrader.ir — a separate shard from the regular api.ibtrader.ir.
    if is_ib:
        shard = runtime_config.get("ib_portfolio_shard", "api8")
        portfolio = f'https://{shard}.{domain}/api/portfolio/getrealsecuritypositionbydate'
        customer_info = f'https://{shard}.{domain}/api/party/getcustomerinfo'
    else:
        portfolio = (
            f'https://backofficeexternal{prefix}{domain}'
            '/api/portfolio/getrealsecuritypositionbydate'
        )
        customer_info = (
            f'https://backofficeexternal{prefix}{domain}'
            '/api/party/getcustomerinfo'
        )

    endpoints = {
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
    # Escape hatch: a per-endpoint full-URL override (``endpoint_<code>_<name>``)
    # redirects a single endpoint verbatim if one path moves on its own.
    for name in list(endpoints):
        override = runtime_config.get(f"endpoint_{code}_{name}", "")
        if override:
            endpoints[name] = override
    return endpoints
