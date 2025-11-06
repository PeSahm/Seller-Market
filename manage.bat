@echo off
REM Trading Bot Management Script
REM Quick commands to manage your trading bot system

echo.
echo 🤖 Trading Bot Management
echo ========================
echo.

if "%1"=="start" goto :start_services
if "%1"=="stop" goto :stop_services
if "%1"=="test" goto :test_system
if "%1"=="cache" goto :run_cache
if "%1"=="trade" goto :run_trade
if "%1"=="status" goto :show_status
if "%1"=="help" goto :show_help

REM No parameter - show menu
echo Debug: Args are %1 %2 %3
echo Select an option:
echo ================
echo 1. Start all services (API + Bot)
echo 2. Stop all services
echo 3. Test system
echo 4. Run cache warmup manually
echo 5. Run trading manually
echo 6. Show system status
echo 7. Show help
echo.

set /p choice="Enter choice (1-7): "

if "%choice%"=="1" goto :start_services
if "%choice%"=="2" goto :stop_services
if "%choice%"=="3" goto :test_system
if "%choice%"=="4" goto :run_cache
if "%choice%"=="5" goto :run_trade
if "%choice%"=="6" goto :show_status
if "%choice%"=="7" goto :show_help

echo Invalid choice
goto :end

:start_services
echo 🚀 Starting Services...
cd SellerMarket

REM Check if API is already running
curl -s http://localhost:5000/health > nul 2>&1
if %errorlevel% neq 0 (
    echo Starting API server...
    start "Config API Server" cmd /c "python config_api.py"
    timeout /t 3 /nobreak > nul
) else (
    echo API server already running
)

REM Check if bot token is set
if "%TELEGRAM_BOT_TOKEN%"=="" (
    if not "%1"=="" (
        set TELEGRAM_BOT_TOKEN=%1
    ) else (
        set /p TELEGRAM_BOT_TOKEN="Enter Telegram Bot Token: "
    )
)
if "%TELEGRAM_USER_ID%"=="" (
    if not "%2"=="" (
        set TELEGRAM_USER_ID=%2
    ) else (
        set /p TELEGRAM_USER_ID="Enter Telegram User ID: "
    )
)

REM Trim trailing spaces
for /f "tokens=* delims= " %%a in ("%TELEGRAM_BOT_TOKEN%") do set TELEGRAM_BOT_TOKEN=%%a
for /f "tokens=* delims= " %%a in ("%TELEGRAM_USER_ID%") do set TELEGRAM_USER_ID=%%a

echo Starting Telegram bot...
start "Telegram Config Bot" cmd /c "cd SellerMarket && set "TELEGRAM_BOT_TOKEN=%TELEGRAM_BOT_TOKEN%" && set "TELEGRAM_USER_ID=%TELEGRAM_USER_ID%" && set "CONFIG_API_URL=http://localhost:5000" && python telegram_config_bot.py && pause"

echo ✅ Services started
goto :end

:stop_services
echo 🛑 Stopping Services...
taskkill /fi "WINDOWTITLE eq Config API Server*" /t /f > nul 2>&1
taskkill /fi "WINDOWTITLE eq Telegram Config Bot*" /t /f > nul 2>&1
echo ✅ Services stopped
goto :end

:test_system
echo 🧪 Testing System...
cd SellerMarket

echo Testing API...
curl -s http://localhost:5000/health | findstr "healthy" > nul
if %errorlevel% neq 0 (
    echo ❌ API server not responding
) else (
    echo ✅ API server healthy
)

echo Testing imports...
python -c "import telebot, flask, requests; print('✅ Python imports OK')" 2> nul
if %errorlevel% neq 0 (
    echo ❌ Python import error
) else (
    echo ✅ Python dependencies OK
)

cd ..
goto :end

:run_cache
echo 🔄 Running Cache Warmup...
cd SellerMarket
python cache_warmup.py
cd ..
goto :end

:run_trade
echo 📈 Running Trading Bot...
cd SellerMarket
locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 40s
cd ..
goto :end

:show_status
echo 📊 System Status
echo ===============

echo Scheduled Tasks:
schtasks /query /tn "TradingBot_CacheWarmup" 2> nul | findstr "Ready" > nul
if %errorlevel% neq 0 (
    echo ❌ Cache warmup task not found
) else (
    echo ✅ Cache warmup task active
)

schtasks /query /tn "TradingBot_OrderExecution" 2> nul | findstr "Ready" > nul
if %errorlevel% neq 0 (
    echo ❌ Order execution task not found
) else (
    echo ✅ Order execution task active
)

echo.
echo Running Processes:
tasklist /fi "imagename eq python.exe" | findstr "python.exe" > nul
if %errorlevel% neq 0 (
    echo ❌ No Python processes running
) else (
    echo ✅ Python processes running
)

echo Checking API server...
curl -s http://localhost:5000/health > nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ API server not responding
) else (
    echo ✅ API server responding
)

echo.
echo API Health:
curl -s http://localhost:5000/health 2> nul
goto :end

:show_help
echo 📚 Help - Trading Bot Management
echo ================================
echo.
echo Usage: manage.bat [command]
echo.
echo Commands:
echo   start    - Start API server and Telegram bot
echo   stop     - Stop all services
echo   test     - Test system components
echo   cache    - Run cache warmup manually
echo   trade    - Run trading bot manually
echo   status   - Show system status
echo   help     - Show this help
echo.
echo Interactive mode: Run without parameters for menu
echo.
echo Examples:
echo   manage.bat start
echo   manage.bat status
echo   manage.bat cache
echo.
goto :end

:end
echo.
pause