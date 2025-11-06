# Trading Bot - Quick Start Guide

## ✅ Setup Complete!

All files have been created and committed. Here's what you have now:

## 📁 New Files

### Windows Service
- **`SellerMarket/trading_service.py`** - Windows service that runs bot + scheduler
- **`SellerMarket/install_service.bat`** - Install service (run as Admin)
- **`SellerMarket/uninstall_service.bat`** - Uninstall service (run as Admin)

### Setup
- **`setup.bat`** - One-command complete setup (recommended!)

### Configuration
- **`SellerMarket/scheduler_config.json`** - Auto-created by setup or bot commands

### Bot Features (Enhanced)
- **`SellerMarket/simple_config_bot.py`** - Now includes:
  - `/cache` - Run cache warmup manually
  - `/trade` - Run trading manually
  - `/status` - System status
  - `/schedule` - Show scheduled jobs
  - `/setcache <HH:MM:SS>` - Set cache warmup time
  - `/settrade <HH:MM:SS>` - Set trading time
  - `/enablejob <name>` - Enable job
  - `/disablejob <name>` - Disable job

## 🚀 How to Use

### First Time Setup

```cmd
setup.bat
```

This will:
1. Install all dependencies
2. Configure Telegram bot (ask for token & user ID)
3. Set up config.ini
4. Create scheduler_config.json (default times: 8:30 AM cache, 8:44:30 AM trade)
5. Optionally install Windows service

### Install as Windows Service

```cmd
cd SellerMarket
install_service.bat
```

**Requirements:** Must run as Administrator

**What it does:**
- Installs TradingBotService
- Sets to auto-start on Windows boot
- Starts the service immediately

**Service includes:**
- Telegram bot (always running)
- Scheduler (runs jobs at configured times)
- Auto-restart on crash

### Manual Usage (No Service)

**Start bot manually:**
```cmd
cd SellerMarket
python simple_config_bot.py
```

**Then use Telegram commands:**
- `/cache` - Run cache warmup
- `/trade` - Run trading

### Telegram Bot Commands

**Configuration:**
```
/list               # List all configs
/add Account2       # Create new config
/use Account2       # Switch to config
/show               # View current config
/broker gs          # Set broker
/symbol IRO1MHRN0001 # Set symbol
```

**Manual Execution:**
```
/cache              # Run cache warmup now
/trade              # Run trading now
/status             # System status
```

**Scheduler:**
```
/schedule           # Show scheduled jobs
/setcache 08:30:00  # Set cache warmup time
/settrade 08:44:30  # Set trading time
/enablejob cache_warmup   # Enable job
/disablejob run_trading   # Disable job
```

## ⏰ Default Schedule

When you run `setup.bat`, it creates this schedule:

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

You can change this via:
- Telegram bot: `/setcache 08:25:00` and `/settrade 08:45:00`
- Or edit `SellerMarket/scheduler_config.json` directly

## 🔄 Daily Workflow

### With Windows Service (Recommended)

**Do nothing!** Service handles everything:
- 8:30:00 AM - Cache warmup runs automatically
- 8:44:30 AM - Trading runs automatically
- Bot stays running 24/7

You can still use Telegram to:
- Check status: `/status`
- Run manually: `/cache` or `/trade`
- Change schedule: `/setcache` / `/settrade`

### Without Windows Service

**Morning:**
```cmd
cd SellerMarket
python simple_config_bot.py
```

**Then via Telegram:**
```
/cache      # Before market opens (around 8:20-8:30)
/trade      # When market opens (8:44:30)
/status     # Check results
```

## 🛠️ Service Management

```cmd
# Start service
net start TradingBotService

# Stop service
net stop TradingBotService

# Check status
sc query TradingBotService

# View logs
type SellerMarket\logs\trading_service.log

# Reinstall
cd SellerMarket
uninstall_service.bat
install_service.bat
```

## 📊 What Gets Preserved

✅ **Manual operations still work:**
```cmd
python cache_warmup.py
python cache_cli.py stats
locust -f locustfile_new.py
```

✅ **Web UI still works:**
```cmd
locust -f locustfile_new.py
# Open http://localhost:8089
```

✅ **All caching features work as before:**
```cmd
python cache_cli.py stats
python cache_cli.py clear
```

## 📖 Documentation

Everything is now in **README.md**:
- Full setup instructions
- All Telegram commands
- Troubleshooting guide
- Performance metrics
- Security warnings

Old separate .md files removed and consolidated.

## 🎯 Key Benefits

1. **One-command setup** - `setup.bat` does everything
2. **Windows service** - Run 24/7, auto-restart
3. **Telegram control** - Configure from anywhere
4. **Automated scheduler** - Set and forget
5. **Manual override** - Always use commands when needed
6. **Preserved functionality** - All old features work

## ⚠️ Important Notes

### Service Installation
- **Must run as Administrator** - Right-click `install_service.bat` → "Run as Administrator"
- **Requires pywin32** - Installed automatically by `setup.bat`

### Scheduler
- **24-hour format** - Use HH:MM:SS (e.g., 08:30:00, not 8:30 AM)
- **Jobs run once per day** - At specified time
- **Configurable via bot** - No need to edit JSON manually

### Manual vs Automated
- **Service running:** Scheduler handles automation, bot always available
- **No service:** Start bot manually, use `/cache` and `/trade` commands
- **Both work:** Choose what fits your workflow

## 🚀 Next Steps

1. **Run setup:**
   ```cmd
   setup.bat
   ```

2. **Test Telegram bot:**
   ```
   /help
   /show
   /status
   ```

3. **Install service (optional but recommended):**
   ```cmd
   cd SellerMarket
   install_service.bat
   ```

4. **Set schedule:**
   ```
   /schedule
   /setcache 08:30:00
   /settrade 08:44:30
   ```

5. **Done!** Service will handle daily trading automatically.

## 📞 Troubleshooting

**See README.md** for complete troubleshooting guide.

Quick checks:
```cmd
# Bot not responding?
type .env                     # Check credentials
net restart TradingBotService # Restart service

# Service won't start?
type SellerMarket\logs\trading_service.log  # Check logs

# Cache issues?
python cache_cli.py stats     # Check cache
python cache_warmup.py        # Manual warmup
```

---

**Ready to go! Run `setup.bat` to get started.** 🚀
