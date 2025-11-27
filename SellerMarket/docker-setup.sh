#!/bin/bash
# Docker Setup Script for Seller-Market Trading Bot
# This script prepares all required files to run the bot using pre-built Docker image

set -e

echo "=============================================="
echo "  Seller-Market Docker Setup"
echo "=============================================="
echo ""

# Create directories
echo "Creating directories..."
mkdir -p logs order_results easyocr_models

# Create empty log files (required for volume mounts)
echo "Creating log files..."
touch trading_bot.log
touch cache_warmup.log

# Create .env file if not exists
if [ ! -f .env ]; then
    echo ""
    echo "Setting up Telegram credentials..."
    read -p "Enter your Telegram Bot Token: " BOT_TOKEN
    read -p "Enter your Telegram User ID: " USER_ID
    
    cat > .env << EOF
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
TELEGRAM_USER_ID=$USER_ID
EOF
    echo "✓ Created .env file"
else
    echo "✓ .env file already exists"
fi

# Create config.ini if not exists
if [ ! -f config.ini ]; then
    cat > config.ini << 'EOF'
# Trading Account Configuration
# Add your trading accounts below

[Account1]
username = YOUR_USERNAME
password = YOUR_PASSWORD
broker = shahr
isin = IRO1MHRN0001
side = 1

# Add more accounts as needed:
# [Account2]
# username = YOUR_USERNAME
# password = YOUR_PASSWORD
# broker = gs
# isin = IRO1FOLD0001
# side = 1
EOF
    echo "✓ Created config.ini (edit with your account details)"
else
    echo "✓ config.ini already exists"
fi

# Create scheduler_config.json if not exists
if [ ! -f scheduler_config.json ]; then
    cat > scheduler_config.json << 'EOF'
{
  "enabled": true,
  "jobs": [
    {
      "name": "cache_warmup",
      "time": "08:30:00",
      "command": "python cache_warmup.py",
      "enabled": true
    },
    {
      "name": "run_trading",
      "time": "08:44:30",
      "command": "locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 30s",
      "enabled": true
    }
  ]
}
EOF
    echo "✓ Created scheduler_config.json"
else
    echo "✓ scheduler_config.json already exists"
fi

# Create locust_config.json if not exists
if [ ! -f locust_config.json ]; then
    cat > locust_config.json << 'EOF'
{
  "locust": {
    "users": 10,
    "spawn_rate": 10,
    "run_time": "30s",
    "host": "https://abc.com",
    "processes": 4
  }
}
EOF
    echo "✓ Created locust_config.json"
else
    echo "✓ locust_config.json already exists"
fi

# Create docker-compose.yml for pre-built image
cat > docker-compose.yml << 'EOF'
# Docker Compose for Seller-Market Trading Bot (Pre-built Image)
# Uses the official image from GitHub Container Registry

services:
  # OCR Service for CAPTCHA solving
  ocr:
    image: ghcr.io/pesahm/ocr:latest
    container_name: seller-market-ocr
    ports:
      - "18080:8080"
      - "15001:5001"
    volumes:
      - ./easyocr_models:/root/.EasyOCR/model
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "python3 -c \"import urllib.request; r=urllib.request.urlopen('http://localhost:5001/health'); exit(0 if b'healthy' in r.read() else 1)\""]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s

  # Trading Bot Service
  trading-bot:
    image: ghcr.io/pesahm/seller-market:latest
    container_name: seller-market-bot
    depends_on:
      ocr:
        condition: service_healthy
    environment:
      - OCR_SERVICE_URL=http://ocr:8080
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - TELEGRAM_USER_ID=${TELEGRAM_USER_ID}
      - TZ=Asia/Tehran
    volumes:
      - ./config.ini:/app/config.ini:ro
      - ./scheduler_config.json:/app/scheduler_config.json:ro
      - ./locust_config.json:/app/locust_config.json:ro
      - ./logs:/app/logs
      - ./order_results:/app/order_results
      - type: bind
        source: ./trading_bot.log
        target: /app/trading_bot.log
      - type: bind
        source: ./cache_warmup.log
        target: /app/cache_warmup.log
    restart: unless-stopped

volumes:
  easyocr_models:
    driver: local

networks:
  default:
    name: seller-market-network
EOF
echo "✓ Created docker-compose.yml"

echo ""
echo "=============================================="
echo "  Setup Complete!"
echo "=============================================="
echo ""
echo "Next steps:"
echo "  1. Edit config.ini with your trading account details"
echo "  2. Review scheduler_config.json for trading times"
echo "  3. Start the bot: docker compose up -d"
echo "  4. View logs: docker compose logs -f trading-bot"
echo ""
echo "Files created:"
echo "  - .env (Telegram credentials)"
echo "  - config.ini (trading accounts)"
echo "  - scheduler_config.json (job scheduler)"
echo "  - locust_config.json (load test config)"
echo "  - docker-compose.yml (container orchestration)"
echo "  - trading_bot.log (trading logs)"
echo "  - cache_warmup.log (cache logs)"
echo "  - logs/ (log directory)"
echo "  - order_results/ (results directory)"
echo "  - easyocr_models/ (OCR model cache)"
echo ""
