@echo off
REM Restart Trading Bot Windows Service
REM Run as Administrator

echo.
echo ===============================================
echo   Restarting Trading Bot Service
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

REM Stop the service if running
echo Stopping service...
net stop TradingBotService 2>nul
timeout /t 3 /nobreak >nul

REM Start the service
echo.
echo Starting service...
net start TradingBotService

echo.
echo Service restarted!
echo.
echo Check logs with:
echo   type logs\trading_service.log
echo.
pause
