@echo off
REM Remote Configuration System Setup Script (Windows)

echo 🤖 Remote Configuration System Setup
echo =====================================

REM Check if we're in the right directory
if not exist "SellerMarket\requirements.txt" (
    echo ❌ Error: Please run this script from the project root directory
    pause
    exit /b 1
)

echo 📦 Installing dependencies...
cd SellerMarket
pip install -r requirements.txt

if %errorlevel% neq 0 (
    echo ❌ Error: Failed to install dependencies
    pause
    exit /b 1
)

echo ✅ Dependencies installed successfully

echo.
echo 🚀 Starting Configuration API Server...
echo This will start the Flask API server on http://localhost:5000
echo Press Ctrl+C to stop the server
echo.

python config_api.py

pause