#!/bin/bash
# Remote Configuration System Setup Script

echo "🤖 Remote Configuration System Setup"
echo "===================================="

# Check if we're in the right directory
if [ ! -f "SellerMarket/requirements.txt" ]; then
    echo "❌ Error: Please run this script from the project root directory"
    exit 1
fi

echo "📦 Installing dependencies..."
cd SellerMarket
pip install -r requirements.txt

if [ $? -ne 0 ]; then
    echo "❌ Error: Failed to install dependencies"
    exit 1
fi

echo "✅ Dependencies installed successfully"

echo ""
echo "🚀 Starting Configuration API Server..."
echo "This will start the Flask API server on http://localhost:5000"
echo "Press Ctrl+C to stop the server"
echo ""

python config_api.py