@echo off
REM Uninstall Trading Bot Windows Service
REM Run as Administrator

echo.
echo ===============================================
echo   Uninstalling Trading Bot Windows Service
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

REM Uninstall the service
echo.
echo Uninstalling service...
python trading_service.py remove

echo.
echo ===============================================
echo   Service uninstalled successfully!
echo ===============================================
echo.
pause
