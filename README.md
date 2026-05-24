# Iranian Stock Market Trading Bot

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Locust](https://img.shields.io/badge/locust-2.0+-green.svg)](https://locust.io/)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue.svg)](https://ghcr.io/pesahm/seller-market)

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
- **Karamad, Tejarat, Ebb** - Configurable

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

### Locust Config (`locust_config.json`)

```json
{
  "locust": {
    "users": 10,
    "spawn_rate": 10,
    "run_time": "30s",
    "host": "https://abc.com",
    "html_report": "report.html"
  }
}
```

**Note:** The `host` parameter is required by Locust CLI even when tasks use absolute URLs. The placeholder URL is ignored by the actual trading tasks, which get their URLs from `broker_enum.py`. This satisfies Locust's framework requirement without affecting the actual API endpoints used.

## 🏃 Running the Bot

## Option 1: Docker (Recommended)

The easiest way to run the bot with all dependencies including the OCR service for CAPTCHA solving.

### Docker Image

Pre-built images are available on GitHub Container Registry with semantic versioning:

```bash
# Latest version
docker pull ghcr.io/pesahm/seller-market:latest

# Specific version
docker pull ghcr.io/pesahm/seller-market:1.2.3

# Major.minor version (auto-updates patches)
docker pull ghcr.io/pesahm/seller-market:1.2

# Major version only (auto-updates minor & patches)
docker pull ghcr.io/pesahm/seller-market:1
```

### Version Tags

Images are automatically tagged based on commit message prefixes:

| Commit Prefix | Version Bump | Example |
|---------------|--------------|---------|  
| `feat:`, `feature:` | Minor (1.0.0 → 1.1.0) | New trading feature |
| `fix:`, `bugfix:` | Patch (1.0.0 → 1.0.1) | Bug fix |
| `breaking:`, `major:` | Major (1.0.0 → 2.0.0) | Breaking change |

### Prerequisites
- Docker and Docker Compose installed
- Configuration files ready

### Quick Start (From Source)

```bash
cd SellerMarket

# Create .env file from example
cp .env.example .env
# Edit .env with your Telegram credentials

# Start all services
docker compose up -d

# View logs
docker compose logs -f trading-bot

# Stop services
docker compose down
```

### Quick Start (Using Pre-built Image)

For users who just want to run the bot without building:

```bash
# Download and run the setup script
curl -O https://raw.githubusercontent.com/PeSahm/Seller-Market/main/SellerMarket/docker-setup.sh
chmod +x docker-setup.sh
./docker-setup.sh

# Or on Windows PowerShell:
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/PeSahm/Seller-Market/main/SellerMarket/docker-setup.ps1" -OutFile "docker-setup.ps1"
.\docker-setup.ps1
```

The setup script will:
- Create required directories and files
- Generate docker-compose.yml for pre-built image
- Create example configuration files
- Prompt for Telegram credentials

### Docker Services

| Service | Description | Ports |
|---------|-------------|-------|
| `ocr` | EasyOCR CAPTCHA solver | 8080, 5001 |
| `trading-bot` | Main trading bot | None (outbound only) |

### Volume Mounts

Configuration files are mounted from host for easy editing:
- `config.ini` - Trading accounts configuration
- `scheduler_config.json` - Scheduler settings
- `locust_config.json` - Locust configuration
- `logs/` - Persistent log storage
- `order_results/` - Trading results

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | Required |
| `TELEGRAM_USER_ID` | Your Telegram user ID | Required |
| `OCR_SERVICE_URL` | OCR service URL | `http://ocr:8080` (Docker) |

## Option 2: Automated Mode (Native)

```cmd
cd SellerMarket
python simple_config_bot.py
```

**Keep this window open!** The bot will:
- ✅ Auto-restart on any errors
- ✅ Run scheduled jobs at configured times
- ✅ Show restart count and status
- ✅ Accept Telegram commands

## Option 3: Manual Mode

```cmd
REM Pre-load cache before market opens
python cache_warmup.py

REM Start Locust when market opens
locust -f locustfile_new.py
REM Open http://localhost:8089

REM Or headless mode
locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 30s
```

## Option 3: Telegram Control

```bash
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
| **Tokens** | 2 hours | Authentication tokens (login is expensive) |
| **Market Data** | 5 minutes | Price limits, volumes |
| **Buying Power** | 5 minutes | Account balance (refreshes so post-credit BP isn't masked by a pre-credit snapshot) |
| **Holdings** | 5 minutes | Per-ISIN share count |
| **Order Params** | 5 minutes | Pre-calculated orders |

### Cache Management

```cmd
REM View cache statistics
python cache_cli.py stats

REM Clean expired entries
python cache_cli.py clean

REM Clear all cache
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
REM Check .env file
type .env

REM Restart bot
Press Ctrl+C in the console
python simple_config_bot.py
```

### Cache not working
```cmd
REM Check status
python cache_cli.py stats

REM Clear and retry
python cache_cli.py clear

REM Manual warmup
python cache_warmup.py
```

### Orders failing
1. Check market hours (9:00-12:30 Tehran time, Sun-Wed)
2. Verify credentials in `config.ini`
3. Check buying power is sufficient
4. Ensure ISIN code is correct
5. Review logs in console or use `/logs` command

## 📁 File Structure

```text
Seller-Market/
├── .env                              # Bot credentials (git-ignored)
├── setup.bat                         # One-command setup
├── README.md                         # This file
├── LICENSE                           # MIT License
└── SellerMarket/
    ├── simple_config_bot.py          # Telegram bot (run this!)
    ├── config.ini                    # Trading accounts config
    ├── scheduler_config.json         # Scheduler configuration
    ├── Dockerfile                    # Docker build configuration
    ├── docker-compose.yml            # Docker Compose orchestration
    ├── .dockerignore                 # Docker build exclusions
    ├── .env.example                  # Environment variables template
    ├── cache_manager.py              # Caching system
    ├── cache_warmup.py               # Pre-market cache loader
    ├── cache_cli.py                  # Cache management CLI
    ├── api_client.py                 # Broker API client
    ├── captcha_utils.py              # OCR CAPTCHA solver
    ├── locustfile_new.py             # Trading bot (Locust)
    ├── requirements.txt              # Python dependencies
    ├── test_docker_config.py         # Docker configuration tests
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
