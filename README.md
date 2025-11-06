
# Iranian Stock Market Trading Bot

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Locust](https://img.shields.io/badge/locust-2.0+-green.svg)](https://locust.io/)

> ⚠️ **SECURITY ALERT**: This repository previously contained exposed credentials. See [SECURITY.md](SECURITY.md) for immediate actions required.

## Overview

A **high-performance automated trading bot** for Iranian stock exchanges (ephoenix.ir platforms). Features intelligent caching, multi-broker support, and dynamic order calculation to eliminate daily manual configuration.

### Key Features
- 🚀 **Instant Execution** - Pre-cached data for zero-latency trading
- 🤖 **Fully Automated** - No manual price/volume updates needed
- 🔄 **Multi-Broker** - Trade across multiple brokers simultaneously
- 📊 **Smart Caching** - 75-90% faster order placement

## 🚀 Quick Start

### Option 1: Complete Automated Setup (Recommended)

```bash
# Clone repository
git clone https://github.com/MostafaEsmaeili/Seller-Market.git
cd Seller-Market

# Run complete setup (includes bot token, API server, Telegram bot, and cron jobs)
complete_setup.bat
```

This script will:
- ✅ Install all dependencies
- ✅ Configure Telegram bot token
- ✅ Start API server and Telegram bot
- ✅ Set up Windows scheduled tasks (8:30 AM cache warmup, 8:44 AM trading)
- ✅ Test the complete system

### Option 2: Manual Setup

```bash
# Clone and setup
git clone https://github.com/MostafaEsmaeili/Seller-Market.git
cd Seller-Market/SellerMarket

# Install dependencies
pip install -r requirements.txt

# Configure accounts
cp config.example.ini config.ini
# Edit config.ini with your credentials

# Pre-load cache (before market opens)
python cache_warmup.py

# Start trading (when market opens)
locust -f locustfile_new.py
# Open http://localhost:8089
```

### Daily Management

Use the management script for daily operations:

```bash
# Start services
manage.bat start

# Check system status
manage.bat status

# Run cache warmup manually
manage.bat cache

# Run trading manually
manage.bat trade

# Stop all services
manage.bat stop
```

📖 **[Read QUICKSTART Guide](QUICKSTART.md)** for detailed setup instructions.

## 🎯 Features

### Multi-Broker Support

- ✅ **Ghadir Shahr (GS)** - identity-gs.ephoenix.ir
- ✅ **Bourse Bazar Iran (BBI)** - identity-bbi.ephoenix.ir
- ✅ **Shahr** - identity-shahr.ephoenix.ir
- 🔄 **Karamad, Tejarat, Shams** - Configurable

### Intelligent Caching System

- � **Token Cache** - 1 hour expiry, auto-refresh
- 📊 **Market Data Cache** - 5 minute expiry for price/volume limits
- 💰 **Buying Power Cache** - 1 minute expiry
- ⚡ **Order Params Cache** - 30 second expiry for pre-calculated orders
- 🔄 **Auto-Cleanup** - Expired entries removed automatically

### Dynamic Order Calculation

- 🎯 **Zero Manual Updates** - Automatically fetches current prices and volumes
- 📈 **Real-time Calculations** - Determines optimal order size based on buying power
- � **Always Current** - No more daily config file edits

### Automation Features

- 🤖 **Automatic Captcha Solving** via OCR service
- 🔐 **Smart Token Management** - Cache-first with auto-refresh
- � **Concurrent Execution** - Multiple accounts simultaneously
- ⚡ **Rate Limit Protection** - Built-in delays to prevent API throttling

## 📋 Requirements

### Software

- Python 3.8+
- Locust 2.0+
- OCR Service (localhost:8080)

### Python Packages

```bash
pip install locust requests
```

## 🔧 Configuration

### Example Config (`config.ini`)

```ini
[Order_Account_Broker]
username = YOUR_ACCOUNT_NUMBER
password = YOUR_PASSWORD
captcha = https://identity-gs.ephoenix.ir/api/Captcha/GetCaptcha
login = https://identity-gs.ephoenix.ir/api/v2/accounts/login
order = https://api-gs.ephoenix.ir/api/v2/orders/NewOrder
editorder = https://api-gs.ephoenix.ir/api/v2/orders/EditOrder
validity = 1           # 1=Day, 2=GTC
side = 1               # 1=Buy, 2=Sell
accounttype = 1
price = 5860
volume = 170017
isin = IRO1MHRN0001
serialnumber = 0       # 0=New order, >0=Edit order
```

## 🏃 Running the Application

### Web Interface (Recommended)

```bash
# Pre-load cache before market opens (8:20 AM)
python cache_warmup.py

# Start Locust when market opens (8:30 AM)
locust -f locustfile_new.py
# Navigate to http://localhost:8089
```

### Headless Mode

```bash
locust -f locustfile_new.py --headless --users 10 --spawn-rate 2 --run-time 1m
```

### Cache Management

```bash
# View cache statistics
python cache_cli.py stats

# Clean expired entries
python cache_cli.py clean

# Clear all cache
python cache_cli.py clear
```

## 📊 Performance

**Order Placement Time:**
- Without caching: 4-6 seconds
- With caching: 0.5-1 second
- **Improvement: 75-90% faster!**

**Cache Expiry Times:**
- Tokens: 1 hour
- Market Data: 5 minutes
- Buying Power: 1 minute
- Order Params: 30 seconds

## 📚 Documentation

- 📖 **[QUICKSTART.md](QUICKSTART.md)** - Quick setup and usage guide
- 🔒 **[SECURITY.md](SECURITY.md)** - Security warnings and best practices
- 🗂️ **[CACHING_IMPLEMENTATION.md](CACHING_IMPLEMENTATION.md)** - Caching system details
- 🔧 **[config.example.ini](SellerMarket/config.example.ini)** - Configuration template

## ⚠️ Security & Legal Warnings

### 🚨 CRITICAL SECURITY ISSUES

- ❌ **Plaintext passwords** in config files
- ❌ **Exposed credentials** in git history
- ❌ **Unencrypted tokens** on disk

**Immediate actions required:**

1. Change all exposed passwords
2. Remove sensitive files from git history
3. Read [SECURITY.md](SECURITY.md) immediately

### ⚖️ Legal Considerations

- **Market Manipulation Risk** - Automated trading may violate regulations
- **Broker ToS** - Check automated trading restrictions
- **Compliance Required** - Consult legal counsel before use

📖 **[Read Full Legal Notice](SECURITY.md)**

## 🤝 Contributing

### Supported Platforms

- [x] Sahra online trading systems (ephoenix platforms)
- [ ] Mofid Securities Orbis trader
- [ ] Rayan online trading system (Exir)
- [ ] Agah online trading system

If you want to contribute:

1. Fork this repository
2. **Never commit credentials or tokens**
3. Test with paper trading accounts
4. Follow PEP 8 style guidelines
5. Submit pull requests with improvements

## ⚠️ Disclaimer

This software is provided **for educational and testing purposes only**.

- ❌ Authors do NOT encourage market manipulation
- ❌ NOT responsible for financial losses
- ❌ NOT responsible for legal consequences
- ❌ NOT liable for security breaches

Users are solely responsible for compliance with all applicable laws and regulations.

## 📞 Support

- 📖 Check documentation files for detailed information
- 🔒 Review SECURITY.md for security concerns
- 🚀 Read QUICKSTART.md for setup help
- 📧 Contact broker support for trading issues
- ⚖️ Consult legal advisor for compliance questions

## 📜 License

This code is released under the [MIT License](LICENSE). Feel free to use and modify this code for your own purposes, as long as you include the original license and attribution.
