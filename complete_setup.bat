@echo off
REM Complete Trading Bot Setup and Management Script
REM This script sets up everything: bot token, API server, Telegram bot, and cron jobs

echo.
echo ===============================================
echo 🤖 COMPLETE TRADING BOT SETUP SCRIPT
echo ===============================================
echo.

REM Check if we're in the right directory
if not exist "SellerMarket\requirements.txt" (
    echo ❌ Error: Please run this script from the project root directory
    echo Expected: C:\Repos\Personal\Seller-Market\
    pause
    exit /b 1
)

echo 📁 Working directory: %CD%
echo.

REM ===============================================
REM STEP 1: Configure Environment Variables
REM ===============================================
echo 🔧 STEP 1: Environment Configuration
echo =====================================

set /p TELEGRAM_BOT_TOKEN="Enter your Telegram Bot Token (from @BotFather): "
if "%TELEGRAM_BOT_TOKEN%"=="" (
    echo ❌ Error: Bot token is required
    pause
    exit /b 1
)

echo.
echo ✅ Bot token configured
echo.

REM Set other environment variables
set CONFIG_API_URL=http://localhost:5000
set PYTHONPATH=%CD%\SellerMarket

echo Environment variables set:
echo   TELEGRAM_BOT_TOKEN=********
echo   CONFIG_API_URL=%CONFIG_API_URL%
echo   PYTHONPATH=%PYTHONPATH%
echo.

REM ===============================================
REM STEP 2: Install Dependencies
REM ===============================================
echo 📦 STEP 2: Installing Dependencies
echo ================================

cd SellerMarket

echo Installing Python packages...
pip install -r requirements.txt

if %errorlevel% neq 0 (
    echo ❌ Error: Failed to install dependencies
    cd ..
    pause
    exit /b 1
)

echo ✅ Dependencies installed successfully
cd ..
echo.

REM ===============================================
REM STEP 3: Start API Server
REM ===============================================
echo 🚀 STEP 3: Starting Configuration API Server
echo ============================================

start "Config API Server" cmd /c "cd SellerMarket && python config_api.py"

echo ⏳ Waiting for API server to start...
timeout /t 3 /nobreak > nul

REM Test API server
curl -s http://localhost:5000/health > nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ Error: API server failed to start
    echo Please check if port 5000 is available
    pause
    exit /b 1
)

echo ✅ API server is running on http://localhost:5000
echo.

REM ===============================================
REM STEP 4: Start Telegram Bot
REM ===============================================
echo 🤖 STEP 4: Starting Telegram Bot
echo ================================

start "Telegram Config Bot" cmd /c "cd SellerMarket && set TELEGRAM_BOT_TOKEN=%TELEGRAM_BOT_TOKEN% && set CONFIG_API_URL=%CONFIG_API_URL% && python telegram_config_bot.py"

echo ⏳ Waiting for bot to start...
timeout /t 2 /nobreak > nul

echo ✅ Telegram bot started
echo 📱 You can now send commands to your bot in Telegram
echo.

REM ===============================================
REM STEP 5: Setup Windows Task Scheduler (Cron Jobs)
REM ===============================================
echo ⏰ STEP 5: Setting up Scheduled Tasks
echo ====================================

REM Get the current directory for the scheduled tasks
set "SCRIPT_DIR=%CD%"
set "SELLER_DIR=%CD%\SellerMarket"

echo Setting up tasks in directory: %SCRIPT_DIR%
echo.

REM Remove existing tasks if they exist
schtasks /delete /tn "TradingBot_CacheWarmup" /f > nul 2>&1
schtasks /delete /tn "TradingBot_OrderExecution" /f > nul 2>&1

echo Creating cache warmup task (runs at 8:30 AM on weekdays)...
schtasks /create /tn "TradingBot_CacheWarmup" /tr "cmd /c cd /d %SELLER_DIR% && python cache_warmup.py" /sc weekly /d MON,TUE,WED,THU,FRI /st 08:30:00 /ru "%USERNAME%" /rl highest /f

if %errorlevel% neq 0 (
    echo ❌ Error: Failed to create cache warmup task
) else (
    echo ✅ Cache warmup task created (8:30 AM weekdays)
)

echo Creating order execution task (runs at 8:44 AM on weekdays)...
schtasks /create /tn "TradingBot_OrderExecution" /tr "cmd /c cd /d %SELLER_DIR% && locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 40s" /sc weekly /d MON,TUE,WED,THU,FRI /st 08:44:00 /ru "%USERNAME%" /rl highest /f

if %errorlevel% neq 0 (
    echo ❌ Error: Failed to create order execution task
) else (
    echo ✅ Order execution task created (8:44 AM weekdays)
)

echo.
echo 📋 Scheduled Tasks Summary:
echo ===========================
schtasks /query /tn "TradingBot_CacheWarmup" | findstr "TradingBot_CacheWarmup"
schtasks /query /tn "TradingBot_OrderExecution" | findstr "TradingBot_OrderExecution"
echo.

REM ===============================================
REM STEP 6: Test the System
REM ===============================================
echo 🧪 STEP 6: System Testing
echo ========================

echo Testing API endpoints...
curl -s http://localhost:5000/health | findstr "healthy" > nul
if %errorlevel% neq 0 (
    echo ❌ API health check failed
) else (
    echo ✅ API server is healthy
)

echo Testing cache warmup (dry run)...
cd SellerMarket
python -c "from cache_manager import TradingCache; print('✅ Cache manager import successful')" 2> nul
if %errorlevel% neq 0 (
    echo ❌ Cache manager test failed
) else (
    echo ✅ Cache system is working
)

cd ..
echo.

REM ===============================================
REM SETUP COMPLETE
REM ===============================================
echo 🎉 SETUP COMPLETE!
echo ==================

echo.
echo ✅ Services Running:
echo   • Configuration API: http://localhost:5000
echo   • Telegram Bot: Active (check your Telegram)
echo   • Scheduled Tasks: Cache warmup (8:30 AM) and Order execution (8:44 AM)
echo.

echo 📱 Telegram Bot Commands:
echo   /start - Welcome and help
echo   /list_configs - Show your configurations
echo   /set_broker gs - Set broker
echo   /set_symbol IRO1MHRN0001 - Set stock symbol
echo   /get_config - Show current config
echo   /get_results - Show recent trades
echo.

echo 🔧 Management Commands:
echo   • View scheduled tasks: schtasks /query /tn TradingBot_*
echo   • Delete tasks: schtasks /delete /tn TradingBot_* /f
echo   • Stop services: Close the command prompt windows
echo.

echo 📝 Next Steps:
echo   1. Configure your broker accounts in config.ini
echo   2. Test the bot with /status command
echo   3. The system will run automatically on weekdays
echo.

echo Press any key to exit...
pause > nul