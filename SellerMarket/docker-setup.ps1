# Docker Setup Script for Seller-Market Trading Bot (PowerShell)
# This script prepares all required files to run the bot using pre-built Docker image

$ErrorActionPreference = "Stop"

Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  Seller-Market Docker Setup" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

# Create directories
Write-Host "Creating directories..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path logs | Out-Null
New-Item -ItemType Directory -Force -Path order_results | Out-Null
New-Item -ItemType Directory -Force -Path easyocr_models | Out-Null

# Create empty log files (required for volume mounts)
Write-Host "Creating log files..." -ForegroundColor Yellow
if (!(Test-Path trading_bot.log)) { New-Item -ItemType File -Name trading_bot.log | Out-Null }
if (!(Test-Path cache_warmup.log)) { New-Item -ItemType File -Name cache_warmup.log | Out-Null }

# Create .env file if not exists
if (!(Test-Path .env)) {
    Write-Host ""
    Write-Host "Setting up Telegram credentials..." -ForegroundColor Yellow
    $botToken = Read-Host "Enter your Telegram Bot Token"
    $userId = Read-Host "Enter your Telegram User ID"
    
    @"
TELEGRAM_BOT_TOKEN=$botToken
TELEGRAM_USER_ID=$userId
"@ | Out-File -FilePath .env -Encoding utf8
    Write-Host "✓ Created .env file" -ForegroundColor Green
} else {
    Write-Host "✓ .env file already exists" -ForegroundColor Green
}

# Create config.ini if not exists
if (!(Test-Path config.ini)) {
    @"
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
"@ | Out-File -FilePath config.ini -Encoding utf8
    Write-Host "✓ Created config.ini (edit with your account details)" -ForegroundColor Green
} else {
    Write-Host "✓ config.ini already exists" -ForegroundColor Green
}

# Create scheduler_config.json if not exists
if (!(Test-Path scheduler_config.json)) {
    @"
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
      "command": "locust -f locustfile_new.py --headless",
      "enabled": true,
      "comment": "Locust parameters (users, spawn-rate, run-time, host) are loaded from locust_config.json"
    }
  ]
}
"@ | Out-File -FilePath scheduler_config.json -Encoding utf8
    Write-Host "✓ Created scheduler_config.json" -ForegroundColor Green
} else {
    Write-Host "✓ scheduler_config.json already exists" -ForegroundColor Green
}

# Create locust_config.json if not exists
if (!(Test-Path locust_config.json)) {
    @"
{
  "locust": {
    "users": 10,
    "spawn_rate": 10,
    "run_time": "30s",
    "host": "https://abc.com",
    "html_report": "report.html",
    "processes": 4,
    "_comment_processes": "Number of worker processes for distributed load. Use -1 for auto-detect CPU cores. Note: --processes requires Linux/macOS (uses fork())"
  }
}
"@ | Out-File -FilePath locust_config.json -Encoding utf8
    Write-Host "✓ Created locust_config.json" -ForegroundColor Green
} else {
    Write-Host "✓ locust_config.json already exists" -ForegroundColor Green
}

# Ask about proxy configuration
Write-Host ""
Write-Host "Proxy Configuration" -ForegroundColor Yellow
Write-Host "------------------" -ForegroundColor Yellow
$useProxy = Read-Host "Do you want to configure a proxy for Telegram? (y/n)"

if ($useProxy -eq "y" -or $useProxy -eq "Y") {
    $proxyUrl = Read-Host "Enter proxy URL (default: http://127.0.0.1:10809)"
    if ([string]::IsNullOrEmpty($proxyUrl)) {
        $proxyUrl = "http://127.0.0.1:10809"
    }
    $noProxyDomains = Read-Host "Enter NO_PROXY domains (default: localhost,127.0.0.1,::1,.ir,tsetmc.com)"
    if ([string]::IsNullOrEmpty($noProxyDomains)) {
        $noProxyDomains = "localhost,127.0.0.1,::1,.ir,tsetmc.com"
    }
    Write-Host "✓ Proxy configured: $proxyUrl" -ForegroundColor Green
    
    # Create docker-compose.yml with proxy (host network mode)
    @"
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
      test: ["CMD", "python3", "-c", "import urllib.request; r=urllib.request.urlopen('http://localhost:5001/health'); exit(0 if b'healthy' in r.read() else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s

  # Trading Bot Service
  trading-bot:
    image: ghcr.io/pesahm/seller-market:latest
    container_name: seller-market-bot
    network_mode: host
    depends_on:
      ocr:
        condition: service_healthy
    environment:
      - OCR_SERVICE_URL=http://127.0.0.1:18080
      - TELEGRAM_BOT_TOKEN=`${TELEGRAM_BOT_TOKEN}
      - TELEGRAM_USER_ID=`${TELEGRAM_USER_ID}
      - TZ=Asia/Tehran
      - HTTP_PROXY=$proxyUrl
      - HTTPS_PROXY=$proxyUrl
      - http_proxy=$proxyUrl
      - https_proxy=$proxyUrl
      - NO_PROXY=$noProxyDomains
      - no_proxy=$noProxyDomains
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
"@ | Out-File -FilePath docker-compose.yml -Encoding utf8
} else {
    Write-Host "✓ No proxy configured - using direct connection" -ForegroundColor Green
    
    # Create docker-compose.yml without proxy
    @"
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
      - TELEGRAM_BOT_TOKEN=`${TELEGRAM_BOT_TOKEN}
      - TELEGRAM_USER_ID=`${TELEGRAM_USER_ID}
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
"@ | Out-File -FilePath docker-compose.yml -Encoding utf8
}
Write-Host "✓ Created docker-compose.yml" -ForegroundColor Green

Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  Setup Complete!" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Edit config.ini with your trading account details"
Write-Host "  2. Review scheduler_config.json for trading times"
Write-Host "  3. Start the bot: docker compose up -d"
Write-Host "  4. View logs: docker compose logs -f trading-bot"
Write-Host ""
Write-Host "Files created:" -ForegroundColor Yellow
Write-Host "  - .env (Telegram credentials)"
Write-Host "  - config.ini (trading accounts)"
Write-Host "  - scheduler_config.json (job scheduler)"
Write-Host "  - locust_config.json (load test config)"
Write-Host "  - docker-compose.yml (container orchestration)"
Write-Host "  - trading_bot.log (trading logs)"
Write-Host "  - cache_warmup.log (cache logs)"
Write-Host "  - logs/ (log directory)"
Write-Host "  - order_results/ (results directory)"
Write-Host "  - easyocr_models/ (OCR model cache)"
Write-Host ""
