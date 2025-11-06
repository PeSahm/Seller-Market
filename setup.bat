@echo off
REM ===============================================
REM Trading Bot - Complete Setup
REM ===============================================
REM This script will:
REM 1. Install all dependencies
REM 2. Configure Telegram bot
REM 3. Set up scheduler
REM 4. Install as Windows service (optional)
REM 5. Test the system
REM ===============================================

echo.
echo ===============================================
echo   TRADING BOT - COMPLETE SETUP
echo ===============================================
echo.

REM Check Python installation
python --version >nul 2>&1
if %errorLevel% neq 0 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.8+ from python.org
    pause
    exit /b 1
)

echo [1/6] Checking Python installation...
python --version
echo.

REM ===============================================
REM Step 1: Install Dependencies
REM ===============================================

echo [2/6] Installing Python dependencies...
echo.

cd SellerMarket

REM Install required packages
pip install locust requests pyTelegramBotAPI python-dotenv pywin32

if %errorLevel% neq 0 (
    echo.
    echo WARNING: Some packages may have failed to install
    echo Please check the error messages above
    pause
)

echo.
echo ✅ Dependencies installed
echo.

REM ===============================================
REM Step 2: Configure Telegram Bot
REM ===============================================

echo [3/6] Configuring Telegram bot...
echo.

REM Check if .env already exists
if exist "..\\.env" (
    echo .env file already exists
    set /p RECREATE="Do you want to reconfigure? (y/n): "
    if /i not "%RECREATE%"=="y" goto SKIP_ENV
)

echo.
echo To get your bot token:
echo 1. Message @BotFather on Telegram
echo 2. Send /newbot and follow instructions
echo 3. Copy the bot token
echo.

set /p BOT_TOKEN="Enter your Telegram Bot Token: "

echo.
echo To get your user ID:
echo 1. Message @userinfobot on Telegram
echo 2. Copy your user ID number
echo.

set /p USER_ID="Enter your Telegram User ID: "

REM Remove quotes and whitespace from token
for /f "tokens=* delims= " %%a in ("%BOT_TOKEN%") do set "BOT_TOKEN=%%a"
set "BOT_TOKEN=%BOT_TOKEN:"=%"

for /f "tokens=* delims= " %%a in ("%USER_ID%") do set "USER_ID=%%a"
set "USER_ID=%USER_ID:"=%"

REM Save to .env file
echo TELEGRAM_BOT_TOKEN=%BOT_TOKEN%> ..\.env
echo TELEGRAM_USER_ID=%USER_ID%>> ..\.env

echo.
echo ✅ Bot configuration saved to .env
echo.

:SKIP_ENV

REM ===============================================
REM Step 3: Configure Trading Accounts
REM ===============================================

echo [4/6] Configuring trading accounts...
echo.

if exist "config.ini" (
    echo config.ini already exists
    set /p RECONFIG="Do you want to reconfigure? (y/n): "
    if /i not "%RECONFIG%"=="y" goto SKIP_CONFIG
)

REM Copy example config
if exist "config.example.ini" (
    copy config.example.ini config.ini
    echo.
    echo ⚠️  Please edit config.ini with your broker credentials
    echo.
    echo Example:
    echo [MyAccount_BrokerName]
    echo username = YOUR_ACCOUNT_NUMBER
    echo password = YOUR_PASSWORD
    echo broker = gs
    echo isin = IRO1MHRN0001
    echo side = 1
    echo.
    set /p EDIT_NOW="Open config.ini now? (y/n): "
    if /i "%EDIT_NOW%"=="y" (
        notepad config.ini
    )
) else (
    echo ⚠️  config.example.ini not found
    echo Please create config.ini manually
)

:SKIP_CONFIG

echo.
echo ✅ Config file ready
echo.

REM ===============================================
REM Step 4: Configure Scheduler
REM ===============================================

echo [5/6] Configuring scheduler...
echo.

if exist "scheduler_config.json" (
    echo Scheduler already configured
    set /p RESCHED="Do you want to reconfigure schedule? (y/n): "
    if /i not "%RESCHED%"=="y" goto SKIP_SCHED
)

echo Default schedule:
echo   - Cache warmup: 08:30:00
echo   - Trading start: 08:44:30
echo.

set /p CUSTOM_SCHED="Use custom times? (y/n): "

if /i "%CUSTOM_SCHED%"=="y" (
    set /p CACHE_TIME="Enter cache warmup time (HH:MM:SS): "
    set /p TRADE_TIME="Enter trading time (HH:MM:SS): "
) else (
    set "CACHE_TIME=08:30:00"
    set "TRADE_TIME=08:44:30"
)

REM Create scheduler config
(
echo {
echo   "enabled": true,
echo   "jobs": [
echo     {
echo       "name": "cache_warmup",
echo       "time": "%CACHE_TIME%",
echo       "command": "python cache_warmup.py",
echo       "enabled": true
echo     },
echo     {
echo       "name": "run_trading",
echo       "time": "%TRADE_TIME%",
echo       "command": "locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 30s",
echo       "enabled": true
echo     }
echo   ]
echo }
) > scheduler_config.json

echo.
echo ✅ Scheduler configured
echo   Cache: %CACHE_TIME%
echo   Trade: %TRADE_TIME%
echo.

:SKIP_SCHED

REM ===============================================
REM Step 5: Install Windows Service (Optional)
REM ===============================================

echo [6/6] Windows Service Installation...
echo.

set /p INSTALL_SERVICE="Install as Windows Service? (y/n): "

if /i "%INSTALL_SERVICE%"=="y" (
    echo.
    echo This requires Administrator privileges
    echo The service will:
    echo   - Start bot automatically on Windows startup
    echo   - Run scheduler in background
    echo   - Restart automatically if it crashes
    echo.
    
    REM Check for admin rights
    net session >nul 2>&1
    if %errorLevel% neq 0 (
        echo ⚠️  This script does not have admin rights
        echo.
        echo To install service:
        echo 1. Close this window
        echo 2. Right-click install_service.bat
        echo 3. Select "Run as Administrator"
        echo.
        pause
        goto SKIP_SERVICE
    )
    
    REM Install service
    python trading_service.py install
    sc config TradingBotService start= auto
    
    set /p START_NOW="Start service now? (y/n): "
    if /i "%START_NOW%"=="y" (
        net start TradingBotService
        echo.
        echo ✅ Service installed and started
    ) else (
        echo.
        echo ✅ Service installed (not started)
        echo To start: net start TradingBotService
    )
) else (
    echo.
    echo Skipping service installation
    echo You can install later by running:
    echo   install_service.bat (as Administrator)
)

:SKIP_SERVICE

echo.
echo.
echo ===============================================
echo   SETUP COMPLETE!
echo ===============================================
echo.

REM ===============================================
REM Summary
REM ===============================================

echo 📋 CONFIGURATION SUMMARY
echo ========================
echo.

if exist "..\.env" (
    echo ✅ Telegram bot configured
) else (
    echo ❌ Telegram bot NOT configured
)

if exist "config.ini" (
    echo ✅ Trading accounts configured
) else (
    echo ❌ Trading accounts NOT configured
)

if exist "scheduler_config.json" (
    echo ✅ Scheduler configured
    type scheduler_config.json | findstr "time"
) else (
    echo ❌ Scheduler NOT configured
)

sc query TradingBotService >nul 2>&1
if %errorLevel% equ 0 (
    echo ✅ Windows service installed
) else (
    echo ⚪ Windows service not installed
)

echo.
echo 📱 TELEGRAM BOT COMMANDS
echo ========================
echo.
echo Start chat with your bot and send:
echo   /help - Show all commands
echo   /show - View current config
echo   /cache - Run cache warmup
echo   /trade - Start trading
echo   /schedule - View scheduled jobs
echo   /status - System status
echo.

echo 🚀 MANUAL USAGE
echo ========================
echo.
echo Run cache warmup:
echo   python cache_warmup.py
echo.
echo Run trading bot:
echo   locust -f locustfile_new.py
echo   Then open: http://localhost:8089
echo.
echo Headless mode:
echo   locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 30s
echo.

echo 🔧 SERVICE MANAGEMENT
echo ========================
echo.

sc query TradingBotService >nul 2>&1
if %errorLevel% equ 0 (
    echo Start service:  net start TradingBotService
    echo Stop service:   net stop TradingBotService
    echo Check status:   sc query TradingBotService
    echo View logs:      type logs\trading_service.log
    echo Uninstall:      uninstall_service.bat
) else (
    echo Install service:  install_service.bat (as Admin)
    echo.
    echo Or run bot manually:
    echo   python simple_config_bot.py
)

echo.
echo ===============================================
echo.

set /p TEST_NOW="Test the bot now? (y/n): "

if /i "%TEST_NOW%"=="y" (
    echo.
    echo Starting bot in test mode...
    echo Press Ctrl+C to stop
    echo.
    echo Send /help to your Telegram bot
    echo.
    pause
    python simple_config_bot.py
)

echo.
echo ===============================================
echo Setup complete! Happy trading! 🚀
echo ===============================================
echo.

cd ..
pause
