@echo off
REM Install Trading Bot as Windows Service
REM Run as Administrator

echo.
echo ===============================================
echo   Installing Trading Bot Windows Service
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

REM Install pywin32 if not already installed
echo Installing dependencies...
pip install pywin32

REM Install the service
echo.
echo Installing service...
python trading_service.py install

REM Set service to auto-start
echo.
echo Configuring service to start automatically...
sc config TradingBotService start= auto

REM Start the service
echo.
echo Starting service...
net start TradingBotService

echo.
echo ===============================================
echo   Service installed successfully!
echo ===============================================
echo.
echo Service Name: TradingBotService
echo Display Name: Trading Bot Service
echo Status: Running
echo Startup Type: Automatic
echo.
echo To manage the service:
echo   - Start:   net start TradingBotService
echo   - Stop:    net stop TradingBotService
echo   - Status:  sc query TradingBotService
echo   - Logs:    logs\trading_service.log
echo.
pause
