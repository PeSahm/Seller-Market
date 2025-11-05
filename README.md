
# Stock Market Load Testing & Automated Trading

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Locust](https://img.shields.io/badge/locust-2.0+-green.svg)](https://locust.io/)

> ⚠️ **SECURITY ALERT**: This repository previously contained exposed credentials. See [SECURITY.md](SECURITY.md) for immediate actions required.

## Overview

A **Locust-based load testing application** designed for high-volume order placement on Iranian stock exchanges (ephoenix.ir platforms). Supports multiple brokers, automatic captcha solving, and concurrent multi-account trading.

### Primary Use Case: Queue Bombing
Rapidly place multiple orders across different brokers to compete in buying queues for high-demand stocks.

## 🚀 Quick Start

```bash
# Clone and setup
git clone https://github.com/MostafaEsmaeili/Seller-Market.git
cd Seller-Market/SellerMarket

# Install dependencies
pip install locust requests

# Configure accounts
cp config.example.ini config.ini
# Edit config.ini with your credentials

# Run load test
locust -f locustfile.py
# Open http://localhost:8089
```

📖 **[Read Full Setup Guide](QUICKSTART.md)**

## 🎯 Features

### Multi-Broker Support
- ✅ **Ghadir Shahr (GS)** - identity-gs.ephoenix.ir
- ✅ **Bourse Bazar Iran (BBI)** - identity-bbi.ephoenix.ir
- ✅ **Shahr** - identity-shahr.ephoenix.ir
- 🔄 **Karamad, Tejarat, Shams** - Configurable

### Automation Capabilities
- 🤖 **Automatic Captcha Solving** via OCR service
- 🔐 **Token Caching** - 2-hour validity, auto-refresh
- 📊 **Concurrent Execution** - Multiple accounts simultaneously
- 🔄 **Order Editing** - Modify existing orders
- ⚡ **High-Speed Execution** - Queue bombing optimized

### Configuration
- 📝 **INI-based Config** - Easy multi-account setup
- 🎛️ **Flexible Parameters** - Price, volume, ISIN, validity
- 🔀 **Dynamic Class Generation** - Auto-create user classes
- 📈 **Real-time Monitoring** - Locust web interface

## 📋 Requirements

### Software
- Python 3.8+
- Locust 2.0+
- OCR Service (localhost:8080)

### Python Packages
```bash
pip install locust requests python-dotenv configparser
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
locust -f locustfile.py
# Navigate to http://localhost:8089
```

### Headless Mode
```bash
locust -f locustfile.py --headless --users 10 --spawn-rate 2 --run-time 1m
```

### Single User Test
```bash
python locustfile.py
```

## 📊 Architecture

### Core Components
1. **Configuration System** - Multi-broker, multi-account support
2. **Authentication** - Captcha solving + token management
3. **Load Testing** - Locust framework with dynamic class generation
4. **Order Execution** - New orders + order editing

### Authentication Flow
```
Load Config → Check Token Cache → [Expired?]
   ↓ Yes                              ↓ No
Fetch Captcha → Decode → Login    Use Cached Token
   ↓                                  ↓
Save Token ────────────────────→ Execute Orders
```

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
- **Market Manipulation Risk** - Queue bombing may violate regulations
- **Broker ToS** - Automated trading restrictions
- **Compliance Required** - Consult legal counsel before use

📖 **[Read Full Legal Notice](SECURITY.md)**

## 📚 Documentation

- 📖 **[QUICKSTART.md](QUICKSTART.md)** - Setup and usage guide
- 🔒 **[SECURITY.md](SECURITY.md)** - Security warnings and best practices
- 📄 **[README_DETAILED.md](README_DETAILED.md)** - Complete technical documentation
- 🔧 **[config.example.ini](SellerMarket/config.example.ini)** - Configuration template

## 🛠️ Technical Stack

- **Language**: Python 3.8+
- **Load Testing**: Locust
- **HTTP Client**: Requests
- **Config**: ConfigParser
- **OCR**: External service (localhost:8080)

## 📈 Performance

### Recommended Settings
- **Queue Bombing**: 20 users, 10/s spawn rate, 30s duration
- **Sustained Trading**: 5 users, 1/s spawn rate, 5m duration
- **Testing**: 1 user, 1/s spawn rate, 1m duration
## 🤝 Contributing

## Tasks

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

This code is released under the [MIT License](https://chat.openai.com/LICENSE). Feel free to use and modify this code for your own purposes, as long as you include the original license and attribution.
