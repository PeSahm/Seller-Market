# Trading Bot Setup Scripts

This directory contains Windows batch files to simplify the setup and management of your trading bot system.

## Files

### `complete_setup.bat`
**Complete one-time setup script** that configures everything:

- Prompts for Telegram bot token
- Installs Python dependencies
- Starts the configuration API server
- Starts the Telegram bot
- Creates Windows scheduled tasks for automated trading
- Tests the complete system

**Usage:**
```bash
complete_setup.bat
```

**What it sets up:**
- Environment variables (`TELEGRAM_BOT_TOKEN`, `CONFIG_API_URL`)
- Windows Task Scheduler tasks:
  - `TradingBot_CacheWarmup` - Runs at 8:30 AM weekdays
  - `TradingBot_OrderExecution` - Runs at 8:44 AM weekdays

### `manage.bat`
**Daily management script** for controlling the trading system:

**Usage:**
```bash
# Interactive menu
manage.bat

# Direct commands
manage.bat start     # Start API server and Telegram bot
manage.bat stop      # Stop all services
manage.bat status    # Show system status
manage.bat test      # Test system components
manage.bat cache     # Run cache warmup manually
manage.bat trade     # Run trading bot manually
```

## System Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Telegram Bot  │◄──►│  Config API     │◄──►│  Trading Bot    │
│                 │    │  (localhost)    │    │  (Locust)       │
│ • Remote config │    │ • REST API      │    │ • Cache warmup  │
│ • Notifications │    │ • JSON storage  │    │ • Order execution│
└─────────────────┘    └─────────────────┘    └─────────────────┘
         ▲                       ▲                       ▲
         │                       │                       │
         └───────────────────────┼───────────────────────┘
                                 ▼
                    ┌─────────────────┐
                    │ Windows Task    │
                    │ Scheduler       │
                    │ (Cron jobs)     │
                    └─────────────────┘
```

## Scheduled Tasks

The setup script creates two Windows scheduled tasks:

1. **Cache Warmup** (8:30 AM weekdays)
   - Pre-loads all trading data
   - Authenticates accounts
   - Caches market data and buying power

2. **Order Execution** (8:44 AM weekdays)
   - Runs the trading bot for 40 seconds
   - Places orders based on cached data
   - Uses 10 concurrent users

## Environment Variables

The scripts set these environment variables:

- `TELEGRAM_BOT_TOKEN` - Your Telegram bot token from @BotFather
- `CONFIG_API_URL` - API server URL (http://localhost:5000)
- `PYTHONPATH` - Python path including SellerMarket directory

## Troubleshooting

### Services won't start
- Check if ports 5000 (API) are available
- Ensure Python dependencies are installed
- Run `manage.bat test` to diagnose issues

### Scheduled tasks not working
- Check Windows Task Scheduler
- Verify user permissions
- Look for task execution logs

### Telegram bot not responding
- Verify bot token is correct
- Check internet connection
- Ensure bot is not blocked by Telegram

## Manual Commands

If you prefer manual control:

```bash
# Start API server
cd SellerMarket
python config_api.py

# Start Telegram bot (in another terminal)
set TELEGRAM_BOT_TOKEN=your_token_here
set CONFIG_API_URL=http://localhost:5000
python telegram_config_bot.py

# Manual cache warmup
python cache_warmup.py

# Manual trading
locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 40s
```

## Security Notes

- Bot tokens are stored in environment variables (not saved to disk)
- Configuration files contain sensitive data - keep them secure
- Review Windows Task Scheduler permissions
- Never commit sensitive data to version control