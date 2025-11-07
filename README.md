# Iranian Stock Market Trading Bot

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Locust](https://img.shields.io/badge/locust-2.0+-green.svg)](https://locust.io/)

> Automated trading bot for Iranian stock exchanges (ephoenix.ir platforms) with Telegram control, intelligent caching, and automated scheduling.
> ⚠️ **SECURITY ALERT**: This repository previously contained exposed credentials. See [SECURITY.md](SECURITY.md) for immediate actions required.
> ⚠️ **LOCALHOST ONLY**: THIS BOT RUNS ON LOCALHOST ONLY — NEVER EXPOSE config_api.py OR THE FLASK SERVICE TO THE INTERNET. The API server is intentionally bound to 127.0.0.1 for security. See [SECURITY.md](SECURITY.md) for deployment guidelines.

## 🚀 Quick Start

```cmd
setup.bat
```

This single script will:
- ✅ Install all dependencies
- ✅ Configure Telegram bot
- ✅ Set up trading accounts
- ✅ Configure scheduler (cache @ 8:30 AM, trade @ 8:44:30 AM)
- ✅ Start the bot

**Keep the console window open** - the bot runs there with auto-restart on errors.

## 🎯 Key Features

### 🤖 Telegram Bot Control
- Configure trading accounts remotely
- Run cache warmup and trading with commands
- View system status and scheduled jobs
- View trading results and logs
- Manage multiple broker accounts
- All from your phone!

### ⏰ Automated Scheduler
- Configurable cron-like scheduler
- Default: Cache @ 8:30 AM, Trade @ 8:44:30 AM
- Edit schedule via Telegram bot or JSON config
- Enable/disable jobs on the fly

### 📊 Intelligent Caching
- 75-90% faster order placement
- Pre-market data preparation
- Automatic expiry management
- CLI tools for cache inspection

### 🏛️ Multi-Broker Support
- **Ghadir Shahr (GS)** - identity-gs.ephoenix.ir
- **Bourse Bazar Iran (BBI)** - identity-bbi.ephoenix.ir
- **Shahr** - identity-shahr.ephoenix.ir
- **Karamad, Tejarat, Shams** - Configurable

### 🎯 Dynamic Order Calculation
- Zero manual price/volume updates
- Automatic buying power calculation
- Real-time market data fetching
- Always uses optimal order size

### 🔄 Auto-Restart
- Bot automatically restarts on errors
- Unlimited retry with exponential backoff
- Never stops working
- Console shows restart count and status

## 📋 Requirements

- **Windows** 10/11 or Server 2016+
- **Python** 3.8 or higher
- **Telegram** account for bot control
- **Active broker account** on ephoenix.ir platform

## 🎯 Telegram Bot Commands

### Configuration Management
| Command | Description | Example |
|---------|-------------|---------|
| `/list` | List all configurations | `/list` |
| `/add <name>` | Create new config | `/add Account2` |
| `/use <name>` | Switch active config | `/use Account2` |
| `/remove <name>` | Delete config | `/remove OldAccount` |
| `/show` | Display current config | `/show` |

### Config Updates
| Command | Description | Example |
|---------|-------------|---------|
| `/broker <code>` | Set broker (gs/bbi/shahr) | `/broker gs` |
| `/symbol <ISIN>` | Set stock symbol | `/symbol IRO1MHRN0001` |
| `/side <1\|2>` | Set buy/sell (1=Buy, 2=Sell) | `/side 1` |
| `/user <username>` | Set username (auto-deleted) | `/user 4580090306` |
| `/pass <password>` | Set password (auto-deleted) | `/pass MyPass123` |

### Manual Execution
| Command | Description |
|---------|-------------|
| `/cache` | Run cache warmup now |
| `/trade` | Start trading bot now |
| `/status` | Show system status |
| `/results` | View latest trading results |
| `/logs [lines]` | View recent log entries (default: 50) |

### Scheduler Management
| Command | Description | Example |
|---------|-------------|---------|
| `/schedule` | Show scheduled jobs | `/schedule` |
| `/setcache <time>` | Set cache warmup time | `/setcache 08:30:00` |
| `/settrade <time>` | Set trading time | `/settrade 08:44:30` |
| `/enablejob <name>` | Enable scheduled job | `/enablejob cache_warmup` |
| `/disablejob <name>` | Disable scheduled job | `/disablejob run_trading` |

## 🔧 Configuration

### Minimal Config (`config.ini`)

```ini
[Account1_Broker]
username = YOUR_ACCOUNT_NUMBER
password = YOUR_PASSWORD
broker = gs
isin = IRO1MHRN0001
side = 1

[Account2_BBI]
username = YOUR_ACCOUNT_NUMBER
password = YOUR_PASSWORD
broker = bbi
isin = IRO1FOLD0001
side = 2
```

**That's it!** Price, volume, endpoints are all calculated automatically.

### Scheduler Config (`scheduler_config.json`)

```json
{
  "enabled": true,
  "jobs": [
    {
      "name": "cache_warmup",
      "time": "08:30:00",
      "command": "python cache_warmup.py",
      "enabled": true
    },
    {
      "name": "run_trading",
      "time": "08:44:30",
      "command": "locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 30s",
      "enabled": true
    }
  ]
}
```

Edit via Telegram bot or directly in JSON file.

## 🏃 Running the Bot

### Start the Bot

```cmd
cd SellerMarket
python simple_config_bot.py
```

**Keep this window open!** The bot will:
- ✅ Auto-restart on any errors
- ✅ Run scheduled jobs at configured times
- ✅ Show restart count and status
- ✅ Accept Telegram commands

### Manual Trading

```cmd
# Pre-load cache before market opens
python cache_warmup.py

# Start Locust when market opens
locust -f locustfile_new.py
# Open http://localhost:8089

# Or headless mode
locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 30s
```

### Via Telegram

```
/cache    # Run cache warmup
/trade    # Start trading
/status   # Check status
/results  # View results
/logs     # View logs
```

## 📊 Cache System

### Cache Types & Expiry

| Type | Expiry | Purpose |
|------|--------|---------|
| **Tokens** | 1 hour | Authentication tokens |
| **Market Data** | 5 minutes | Price limits, volumes |
| **Buying Power** | 1 minute | Account balance |
| **Order Params** | 30 seconds | Pre-calculated orders |

### Cache Management

```cmd
# View cache statistics
python cache_cli.py stats

# Clean expired entries
python cache_cli.py clean

# Clear all cache
python cache_cli.py clear
```

### Performance Impact

**Without Caching:**
- Authentication: 2-3 seconds
- Get Buying Power: 0.5-1 second
- Get Market Data: 0.5-1 second
- **Total: 4-6 seconds per order**

**With Caching:**
- All cached data: ~0ms
- Order placement only: 0.5-1 second
- **Total: 0.5-1 second per order**
- **Improvement: 75-90% faster!**

## 🛠️ Troubleshooting

### Bot doesn't respond
```cmd
# Check .env file
type .env

# Restart bot
Press Ctrl+C in the console
python simple_config_bot.py
```

### Cache not working
```cmd
python cache_cli.py stats       # Check status
python cache_cli.py clear       # Clear and retry
python cache_warmup.py          # Manual warmup
```

### Orders failing
1. Check market hours (9:00-12:30 Tehran time, Sun-Wed)
2. Verify credentials in `config.ini`
3. Check buying power is sufficient
4. Ensure ISIN code is correct
5. Review logs in console or use `/logs` command

## 📁 File Structure

```
Seller-Market/
├── .env                              # Bot credentials (git-ignored)
├── setup.bat                         # One-command setup
├── README.md                         # This file
├── LICENSE                           # MIT License
└── SellerMarket/
    ├── simple_config_bot.py          # Telegram bot (run this!)
    ├── config.ini                    # Trading accounts config
    ├── scheduler_config.json         # Scheduler configuration
    ├── cache_manager.py              # Caching system
    ├── cache_warmup.py               # Pre-market cache loader
    ├── cache_cli.py                  # Cache management CLI
    ├── api_client.py                 # Broker API client
    ├── locustfile_new.py             # Trading bot (Locust)
    ├── requirements.txt              # Python dependencies
    └── logs/
        ├── trading_bot.log           # Bot logs
        ├── bot_output.log            # Console output
        └── cache_warmup.log          # Cache logs
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

- **Market Manipulation Risk** - Automated trading may violate regulations
- **Broker ToS** - Check automated trading restrictions
- **Compliance Required** - Consult legal counsel before use

📖 **[Read Full Legal Notice](SECURITY.md)**

## ⚠️ Disclaimer

This software is provided **for educational and testing purposes only**.

- ❌ Authors do NOT encourage market manipulation
- ❌ NOT responsible for financial losses
- ❌ NOT responsible for legal consequences
- ❌ NOT liable for security breaches

Users are solely responsible for compliance with all applicable laws and regulations.

## 📊 Performance Metrics

- **Order Placement:** 0.5-1 second (with cache)
- **Cache Hit Rate:** 90%+ (after warmup)
- **API Call Reduction:** 95%
- **Concurrent Accounts:** Tested with 10+
- **Bot Uptime:** Unlimited auto-restart

## 🤝 Contributing

Contributions welcome! Please:

1. Fork the repository
2. **Never commit credentials**
3. Test thoroughly with paper trading
4. Follow PEP 8 style guidelines
5. Submit pull request with clear description

## 📜 License

MIT License - See [LICENSE](LICENSE) file for details.

---

## 🎉 Summary

This trading bot provides:

✅ **One-command setup** - `setup.bat` does everything  
✅ **Telegram control** - Configure and execute from phone  
✅ **Auto-restart** - Never stops on errors  
✅ **Automated scheduling** - Set and forget daily trading  
✅ **Intelligent caching** - 75-90% performance improvement  
✅ **Multi-broker/account** - Trade across multiple platforms  
✅ **Dynamic calculation** - No manual price/volume updates  
✅ **Comprehensive logging** - Full audit trail  

**Result:** Professional-grade automated trading system with minimal configuration and maximum flexibility.

---

**Ready to start?** 

1. Run `setup.bat`
2. Keep the console window open
3. Send `/help` to your Telegram bot

**Happy trading! 🚀**


> ⚠️ **SECURITY ALERT**: This repository previously contained exposed credentials. See [SECURITY.md](SECURITY.md) for immediate actions required.

## 🚀 Quick Start - One Command Setup

## Overview

```cmd

setup.batA **high-performance automated trading bot** for Iranian stock exchanges (ephoenix.ir platforms). Features intelligent caching, multi-broker support, and dynamic order calculation to eliminate daily manual configuration.

```

### Key Features

This single script will:- 🚀 **Instant Execution** - Pre-cached data for zero-latency trading

- ✅ Install all dependencies- 🤖 **Fully Automated** - No manual price/volume updates needed

- ✅ Configure Telegram bot- 🔄 **Multi-Broker** - Trade across multiple brokers simultaneously

- ✅ Set up trading accounts- 📊 **Smart Caching** - 75-90% faster order placement

- ✅ Configure scheduler (cache @ 8:30 AM, trade @ 8:44:30 AM)

- ✅ Test the system

### Option 1: Complete Automated Setup (Recommended)

**That's it!** Your trading bot is ready.

```bash

## 🎯 Key Features# Clone repository

git clone https://github.com/MostafaEsmaeili/Seller-Market.git

### 🤖 Telegram Bot Controlcd Seller-Market

- Configure trading accounts remotely

- Run cache warmup and trading manually with commands# Run complete setup (includes bot token, API server, Telegram bot, and cron jobs)

- View system status and scheduled jobscomplete_setup.bat

- Manage multiple broker accounts```

- All from your phone!

This script will:

### ⏰ Automated Scheduler- ✅ Install all dependencies

- Configurable cron-like scheduler- ✅ Configure Telegram bot token

- Default: Cache @ 8:30 AM, Trade @ 8:44:30 AM- ✅ Start API server and Telegram bot

- Edit schedule via Telegram bot or JSON config- ✅ Set up Windows scheduled tasks (8:30 AM cache warmup, 8:44 AM trading)

- Enable/disable jobs on the fly- ✅ Test the complete system



### 🔧 Manual Operation### Option 2: Manual Setup

- Runs bot and scheduler as Windows service

- Auto-start on boot```bash

- Auto-restart on crash# Clone and setup

- Persistent background operationgit clone https://github.com/MostafaEsmaeili/Seller-Market.git

cd Seller-Market/SellerMarket

### 📊 Intelligent Caching

- 75-90% faster order placement# Install dependencies

- Pre-market data preparationpip install -r requirements.txt

- Automatic expiry management

- CLI tools for cache inspection# Configure accounts

cp config.example.ini config.ini

### 🏛️ Multi-Broker Support# Edit config.ini with your credentials

- **Ghadir Shahr (GS)** - identity-gs.ephoenix.ir

- **Bourse Bazar Iran (BBI)** - identity-bbi.ephoenix.ir# Pre-load cache (before market opens)

- **Shahr** - identity-shahr.ephoenix.irpython cache_warmup.py

- **Karamad, Tejarat, Shams** - Configurable

# Start trading (when market opens)

### 🎯 Dynamic Order Calculationlocust -f locustfile_new.py

- Zero manual price/volume updates# Open http://localhost:8089

- Automatic buying power calculation```

- Real-time market data fetching

- Always uses optimal order size### Daily Management



## 📋 RequirementsUse the management script for daily operations:



- **Windows** 10/11 or Server 2016+```bash

- **Python** 3.8 or higher# Start services

- **Telegram** account for bot controlmanage.bat start

- **Active broker account** on ephoenix.ir platform

# Check system status

## 🎯 Telegram Bot Commandsmanage.bat status



### Configuration Management# Run cache warmup manually

| Command | Description | Example |manage.bat cache

|---------|-------------|---------|

| `/list` | List all configurations | `/list` |# Run trading manually

| `/add <name>` | Create new config | `/add Account2` |manage.bat trade

| `/use <name>` | Switch active config | `/use Account2` |

| `/remove <name>` | Delete config | `/remove OldAccount` |# Stop all services

| `/show` | Display current config | `/show` |manage.bat stop

```

### Config Updates

| Command | Description | Example |📖 **[Read QUICKSTART Guide](QUICKSTART.md)** for detailed setup instructions.

|---------|-------------|---------|

| `/broker <code>` | Set broker (gs/bbi/shahr) | `/broker gs` |## 🎯 Features

| `/symbol <ISIN>` | Set stock symbol | `/symbol IRO1MHRN0001` |

| `/side <1\|2>` | Set buy/sell (1=Buy, 2=Sell) | `/side 1` |### Multi-Broker Support

| `/user <username>` | Set username (auto-deleted) | `/user 4580090306` |

| `/pass <password>` | Set password (auto-deleted) | `/pass MyPass123` |- ✅ **Ghadir Shahr (GS)** - identity-gs.ephoenix.ir

- ✅ **Bourse Bazar Iran (BBI)** - identity-bbi.ephoenix.ir

### Manual Execution

| Command | Description |
|---------|-------------|
| `/cache` | Run cache warmup now |
| `/trade` | Start trading bot now |
| `/status` | Show system status |
| `/results` | View latest trading results |
| `/logs [lines]` | View recent log entries (default: 50) |

### Scheduler Management

| Command | Description | Example |- ⚡ **Order Params Cache** - 30 second expiry for pre-calculated orders

|---------|-------------|---------|- 🔄 **Auto-Cleanup** - Expired entries removed automatically

| `/schedule` | Show scheduled jobs | `/schedule` |

| `/setcache <time>` | Set cache warmup time | `/setcache 08:30:00` |### Dynamic Order Calculation

| `/settrade <time>` | Set trading time | `/settrade 08:44:30` |

| `/enablejob <name>` | Enable scheduled job | `/enablejob cache_warmup` |- 🎯 **Zero Manual Updates** - Automatically fetches current prices and volumes

| `/disablejob <name>` | Disable scheduled job | `/disablejob run_trading` |- 📈 **Real-time Calculations** - Determines optimal order size based on buying power

- � **Always Current** - No more daily config file edits

## 🔧 Configuration

### Automation Features

### Minimal Config (`config.ini`)

- 🤖 **Automatic Captcha Solving** via OCR service

```ini- 🔐 **Smart Token Management** - Cache-first with auto-refresh

[Account1_Broker]- � **Concurrent Execution** - Multiple accounts simultaneously

username = YOUR_ACCOUNT_NUMBER- ⚡ **Rate Limit Protection** - Built-in delays to prevent API throttling

password = YOUR_PASSWORD

broker = gs## 📋 Requirements

isin = IRO1MHRN0001

side = 1### Software



[Account2_BBI]- Python 3.8+

username = YOUR_ACCOUNT_NUMBER- Locust 2.0+

password = YOUR_PASSWORD- OCR Service (localhost:8080)

broker = bbi

isin = IRO1FOLD0001### Python Packages

side = 2

``````bash

pip install locust requests

**That's it!** Price, volume, endpoints are all calculated automatically.```



### Scheduler Config (`scheduler_config.json`)## 🔧 Configuration



```json### Example Config (`config.ini`)

{

  "enabled": true,```ini

  "jobs": [[Order_Account_Broker]

    {username = YOUR_ACCOUNT_NUMBER

      "name": "cache_warmup",password = YOUR_PASSWORD

      "time": "08:30:00",captcha = https://identity-gs.ephoenix.ir/api/Captcha/GetCaptcha

      "command": "python cache_warmup.py",login = https://identity-gs.ephoenix.ir/api/v2/accounts/login

      "enabled": trueorder = https://api-gs.ephoenix.ir/api/v2/orders/NewOrder

    },editorder = https://api-gs.ephoenix.ir/api/v2/orders/EditOrder

    {validity = 1           # 1=Day, 2=GTC

      "name": "run_trading",side = 1               # 1=Buy, 2=Sell

      "time": "08:44:30",accounttype = 1

      "command": "locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 30s",price = 5860

      "enabled": truevolume = 170017

    }isin = IRO1MHRN0001

  ]serialnumber = 0       # 0=New order, >0=Edit order

}```

```

## 🏃 Running the Application

Edit via Telegram bot or directly in JSON file.

### Web Interface (Recommended)

## 🏃 Running the System

```bash

### Option 1: Manual Operation (Recommended)# Pre-load cache before market opens (8:20 AM)

python cache_warmup.py

```cmd

cd SellerMarket# Start Locust when market opens (8:30 AM)

install_service.batlocust -f locustfile_new.py

```# Navigate to http://localhost:8089

```

The service will:

- Run bot manually with auto-restart on errors### Headless Mode

- Scheduler runs in background thread

- Execute cache warmup and trading at scheduled times```bash

- Monitor via Telegram bot and console outputlocust -f locustfile_new.py --headless --users 10 --spawn-rate 2 --run-time 1m

```



**Manage service:**### Cache Management

```cmd

net start TradingBotService    # Start```bash

net stop TradingBotService     # Stop# View cache statistics

sc query TradingBotService     # Statuspython cache_cli.py stats

```

# Clean expired entries

### Option 2: Manual Modepython cache_cli.py clean



**Start bot:**# Clear all cache

```cmdpython cache_cli.py clear

cd SellerMarket```

python simple_config_bot.py

```## 📊 Performance



**Run cache warmup:****Order Placement Time:**

```cmd- Without caching: 4-6 seconds

python cache_warmup.py- With caching: 0.5-1 second

```- **Improvement: 75-90% faster!**



**Run trading:****Cache Expiry Times:**

```cmd- Tokens: 1 hour

locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 30s- Market Data: 5 minutes

```- Buying Power: 1 minute

- Order Params: 30 seconds

Or via Telegram:

- Send `/cache` to run cache warmup## 📚 Documentation

- Send `/trade` to start trading

- 📖 **[QUICKSTART.md](QUICKSTART.md)** - Quick setup and usage guide

## 📊 Cache System- 🔒 **[SECURITY.md](SECURITY.md)** - Security warnings and best practices

- 🗂️ **[CACHING_IMPLEMENTATION.md](CACHING_IMPLEMENTATION.md)** - Caching system details

### Cache Types & Expiry- 🔧 **[config.example.ini](SellerMarket/config.example.ini)** - Configuration template



| Type | Expiry | Purpose |## ⚠️ Security & Legal Warnings

|------|--------|---------|

| **Tokens** | 1 hour | Authentication tokens |### 🚨 CRITICAL SECURITY ISSUES

| **Market Data** | 5 minutes | Price limits, volumes |

| **Buying Power** | 1 minute | Account balance |- ❌ **Plaintext passwords** in config files

| **Order Params** | 30 seconds | Pre-calculated orders |- ❌ **Exposed credentials** in git history

- ❌ **Unencrypted tokens** on disk

### Cache Management

**Immediate actions required:**

```cmd

# View cache statistics1. Change all exposed passwords

python cache_cli.py stats2. Remove sensitive files from git history

3. Read [SECURITY.md](SECURITY.md) immediately

# Clean expired entries

python cache_cli.py clean### ⚖️ Legal Considerations



# Clear all cache- **Market Manipulation Risk** - Automated trading may violate regulations

python cache_cli.py clear- **Broker ToS** - Check automated trading restrictions

- **Compliance Required** - Consult legal counsel before use

# Clear specific type

python cache_cli.py clear tokens📖 **[Read Full Legal Notice](SECURITY.md)**

```

## 🤝 Contributing

### Performance Impact

### Supported Platforms

**Without Caching:**

- Authentication: 2-3 seconds- [x] Sahra online trading systems (ephoenix platforms)

- Get Buying Power: 0.5-1 second- [ ] Mofid Securities Orbis trader

- Get Market Data: 0.5-1 second- [ ] Rayan online trading system (Exir)

- **Total: 4-6 seconds per order**- [ ] Agah online trading system



**With Caching:**If you want to contribute:

- All cached data: ~0ms

- Order placement only: 0.5-1 second1. Fork this repository

- **Total: 0.5-1 second per order**2. **Never commit credentials or tokens**

- **Improvement: 75-90% faster!**3. Test with paper trading accounts

4. Follow PEP 8 style guidelines

## 🔒 Security Features5. Submit pull requests with improvements



- ✅ **Auto-delete** - Credential messages deleted automatically## ⚠️ Disclaimer

- ✅ **User authorization** - Only your Telegram ID can control bot

- ✅ **.env storage** - Sensitive data not in gitThis software is provided **for educational and testing purposes only**.

- ⚠️ **Note:** Credentials in `config.ini` are plain text - secure your file system!- ❌ Authors do NOT encourage market manipulation

- ❌ NOT responsible for financial losses

## 🕐 Daily Workflow- ❌ NOT responsible for legal consequences

- ❌ NOT liable for security breaches

### Automated (Manual Operation)

Users are solely responsible for compliance with all applicable laws and regulations.

**Keep console window open!** The bot handles everything automatically:

- 8:30:00 AM - Cache warmup runs automatically## 📞 Support

- 8:44:30 AM - Trading starts automatically

- 📖 Check documentation files for detailed information

Check Telegram for notifications.- 🔒 Review SECURITY.md for security concerns

- 🚀 Read QUICKSTART.md for setup help

### Manual Control- 📧 Contact broker support for trading issues

- ⚖️ Consult legal advisor for compliance questions

**Morning (before market):**

```## 📜 License

Send to Telegram bot:

/status          # Check systemThis code is released under the [MIT License](LICENSE). Feel free to use and modify this code for your own purposes, as long as you include the original license and attribution.

/cache           # Warm up cache
```

**Market opens:**
```
Send to Telegram bot:
/trade           # Start trading
```

**After trading:**
```
Send to Telegram bot:
/status          # Check results
```

## 🛠️ Troubleshooting

### Bot doesn't respond
```cmd
# Check .env file
type .env

# Verify bot token
python -c "import os; from dotenv import load_dotenv; load_dotenv('.env'); print(os.getenv('TELEGRAM_BOT_TOKEN'))"

# Restart bot
Press Ctrl+C in console, then run:
python simple_config_bot.py
```


```cmd
cd SellerMarket
python cache_cli.py stats       # Check status
python cache_cli.py clear       # Clear and retry
python cache_warmup.py          # Manual warmup
```

### Orders failing
1. Check market hours (9:00-12:30 Tehran time, Sun-Wed)
2. Verify credentials in `config.ini`
3. Check buying power is sufficient
4. Ensure ISIN code is correct
5. Review logs: `type SellerMarket\logs\trading_service.log`

## 📁 File Structure

```
Seller-Market/
├── .env                              # Bot credentials (git-ignored)
├── setup.bat                         # One-command setup
├── README.md                         # This file
├── LICENSE                           # MIT License
└── SellerMarket/
    ├── simple_config_bot.py          # Telegram bot
    ├── config.ini                    # Trading accounts config
    ├── scheduler_config.json         # Scheduler configuration
    ├── cache_manager.py              # Caching system
    ├── cache_warmup.py               # Pre-market cache loader
    ├── cache_cli.py                  # Cache management CLI
    ├── api_client.py                 # Broker API client
    ├── locustfile_new.py             # Trading bot (Locust)
    ├── requirements.txt              # Python dependencies
    └── logs/
        ├── trading_service.log       # Service logs
        ├── trading_bot.log           # Bot logs
        └── cache_warmup.log          # Cache logs
```

## ⚠️ Security & Legal Warnings

### 🚨 Security Issues

- ❌ **Plaintext passwords** in config.ini
- ❌ **Unencrypted tokens** in cache
- ⚠️ **Sensitive data** in logs

**Recommendations:**
1. Secure file system permissions
2. Use dedicated trading account with limited funds
3. Keep `.env` and `config.ini` out of git (already configured)
4. Regularly review access logs

### ⚖️ Legal Considerations

- **Market Manipulation Risk** - Automated trading may violate regulations
- **Broker ToS** - Check if automated trading is allowed
- **Compliance** - Consult legal counsel before production use

**Disclaimer:** This software is for **educational purposes only**. Authors are NOT responsible for financial losses, legal consequences, or security breaches.

## 📊 Performance Metrics

- **Order Placement:** 0.5-1 second (with cache)
- **Cache Hit Rate:** 90%+ (after warmup)
- **API Call Reduction:** 95%
- **Concurrent Accounts:** Tested with 10+
- **Concurrent Orders:** Tested with 100+

## 🎓 Advanced Usage

### Custom Scheduler

Edit `scheduler_config.json` or use bot commands:

```
/setcache 08:25:00     # Earlier cache warmup
/settrade 08:44:45     # Later trading start
```

### High-Frequency Trading

```cmd
# Run trading every 30 seconds for 5 minutes
locust -f locustfile_new.py --headless --users 20 --spawn-rate 10 --run-time 5m
```

### Multiple ISINs

Create separate configs for each symbol:

```ini
[Account1_MHRN]
broker = gs
isin = IRO1MHRN0001
side = 1

[Account1_FOLD]
broker = gs
isin = IRO1FOLD0001
side = 2
```

Switch between them:
```
/use Account1_MHRN
/use Account1_FOLD
```

## 🤝 Contributing

Contributions welcome! Please:

1. Fork the repository
2. **Never commit credentials**
3. Test thoroughly with paper trading
4. Follow PEP 8 style guidelines
5. Submit pull request with clear description

## 📞 Support

- 📖 Read this README thoroughly
- 🔍 Check troubleshooting section
- 📁 Review log files in `SellerMarket/logs/`
- 💬 Use Telegram bot `/status` and `/help` commands
- 🐛 Report issues on GitHub (without credentials!)

## 🚀 Roadmap

- [ ] Web dashboard for monitoring
- [ ] Multi-ISIN per account
- [ ] Portfolio rebalancing strategies
- [ ] Stop-loss automation
- [ ] Machine learning for timing
- [ ] Mobile app for monitoring
- [ ] Docker containerization
- [ ] Redis caching for distributed setups

## 📜 License

MIT License - See [LICENSE](LICENSE) file for details.

---

## 🎉 Summary

This trading bot provides:

✅ **One-command setup** - `setup.bat` does everything  
✅ **Telegram control** - Configure and execute from phone  
✅ **Manual operation** - Run with auto-restart and background scheduling  
✅ **Automated scheduling** - Set and forget daily trading  
✅ **Intelligent caching** - 75-90% performance improvement  
✅ **Multi-broker/account** - Trade across multiple platforms  
✅ **Dynamic calculation** - No manual price/volume updates  
✅ **Comprehensive logging** - Full audit trail  

**Result:** Professional-grade automated trading system with minimal configuration and maximum flexibility.

---

**Ready to start?** Run `setup.bat` and send `/help` to your Telegram bot!

**Happy trading! 🚀**
