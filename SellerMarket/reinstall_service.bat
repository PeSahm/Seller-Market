@echo off
REM Completely reinstall Trading Bot Service
REM Run as Administrator

echo.
echo ===============================================
echo   Reinstalling Trading Bot Service
echo ===============================================
echo.

REM Check for admin rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo ERROR: This script requires Administrator privileges
    echo Please right-click and select "Run as Administrator"
    pause
    exit /b 1
)

echo Step 1: Stopping service...
net stop TradingBotService 2>nul

echo.
echo Step 2: Removing old service...
python trading_service.py remove 2>nul

echo.
echo Step 3: Waiting for cleanup...
echo Please close any open Services windows (services.msc) if you have them open.
echo.
timeout /t 5 /nobreak

echo.
echo Step 4: Installing new service...
python trading_service.py install

if %errorLevel% neq 0 (
    echo.
    echo ERROR: Service installation failed!
    echo Try restarting your computer and run this script again.
    pause
    exit /b 1
)

echo.
echo Step 5: Configuring service to run as current user...
echo This allows the bot to have network access
sc config TradingBotService obj= ".\%USERNAME%" password= ""

echo.
echo Step 6: Setting auto-start...
sc config TradingBotService start= auto

echo.
echo Step 7: Starting service...
net start TradingBotService
net start TradingBotService

if %errorLevel% equ 0 (
    echo.
    echo ===============================================
    echo   Service installed successfully!
    echo ===============================================
    echo.
    echo Service is now running with network access
    echo.
    echo Check logs:
    echo   - Service: logs\trading_service.log
    echo   - Bot:     logs\bot_output.log
    echo.
    echo Test your bot on Telegram now!
) else (
    echo.
    echo WARNING: Service failed to start
    echo Check logs\trading_service.log for errors
)

echo.
pause
