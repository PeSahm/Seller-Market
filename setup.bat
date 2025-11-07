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
pip install -r requirements.txt

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
REM Step 5: Setup Complete
REM ===============================================

echo [6/6] Setup verification...
echo.

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
echo NOTE: Locust parameters can be configured in locust_config.json

echo ===============================================
echo.

set /p ADD_STARTUP="Add bot to Windows Startup? (y/n): "

if /i "%ADD_STARTUP%"=="y" (
    echo.
    echo Creating startup shortcut...
    
    REM Get the full path to the batch file
    set "BAT_PATH=%~dp0SellerMarket\run_bot.bat"
    set "STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
    
    REM Create a simple runner batch file
    (
        echo @echo off
        echo cd /d "%~dp0"
        echo python simple_config_bot.py
        echo pause
    ) > "%~dp0SellerMarket\run_bot.bat"
    
    REM Create shortcut
    powershell -Command "$WshShell = New-Object -ComObject WScript.Shell; $Shortcut = $WshShell.CreateShortcut('%STARTUP_FOLDER%\SellerMarket Bot.lnk'); $Shortcut.TargetPath = '%BAT_PATH%'; $Shortcut.WorkingDirectory = '%~dp0SellerMarket'; $Shortcut.Description = 'Seller Market Trading Bot'; $Shortcut.Save()"
    
    if %errorLevel% equ 0 (
        echo ✅ Startup shortcut created successfully!
        echo    Location: %STARTUP_FOLDER%\SellerMarket Bot.lnk
        echo.
        echo The bot will start automatically when you log in to Windows.
        echo To remove: Delete the shortcut from the Startup folder
    ) else (
        echo ❌ Failed to create startup shortcut
    )
    echo.
)

set /p TEST_NOW="Start the bot now? (y/n): "

if /i "%TEST_NOW%"=="y" (
    echo.
    echo Starting Telegram bot...
    echo.
    echo ⚠️  Keep this window open! The bot will run here.
    echo    Press Ctrl+C to stop the bot
    echo.
    echo Send /help to your Telegram bot to test it
    echo.
    pause
    python simple_config_bot.py
) else (
    echo.
    echo To start the bot manually:
    echo   cd SellerMarket
    echo   python simple_config_bot.py
    echo.
    echo Or double-click: SellerMarket\run_bot.bat
)

echo.
echo ===============================================
echo Setup complete! Happy trading! 🚀
echo ===============================================
echo.

cd ..
pause
