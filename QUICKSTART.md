# Quick Start Guide

Get your trading bot running in 5 minutes!

## Prerequisites

- Python 3.8+
- pip (Python package manager)
- Active broker account on ephoenix.ir platform

## Step 1: Install Dependencies

```bash
cd Seller-Market/SellerMarket
pip install locust requests
```

## Step 2: Configure Your Accounts

1. Copy the example configuration:

```bash
cp config.example.ini config.ini
```

2. Edit `config.ini` with your broker credentials:

```ini
[MyAccount_BrokerName]
username = YOUR_ACCOUNT_NUMBER
password = YOUR_PASSWORD
broker_code = shahr
isin = IRO1MHRN0001
```

**That's it!** The bot will automatically:
- ✅ Fetch current market prices and limits
- ✅ Calculate optimal order volume based on your buying power
- ✅ Use correct broker endpoints
- ✅ No manual price/volume updates needed!

**Broker codes:** `gs`, `bbi`, `shahr`, `karamad`, `tejarat`, `shams`

**Important:** Never commit `config.ini` to git!

## Step 3: Pre-Load Cache (Before Market Opens)

Run this **5-10 minutes before market opens** (e.g., 8:20 AM):

```bash
python cache_warmup.py
```

This will:
- ✅ Authenticate all accounts
- ✅ Fetch current buying power
- ✅ Get instrument price/volume limits
- ✅ Pre-calculate order parameters
- ✅ Cache everything for instant access

Expected output:

```
✓✓✓ Cache warmup successful for 4580090306@shahr ✓✓✓
Cache Statistics:
  - Total entries: 7
  - Tokens: 2 (expires in 1 hour)
  - Market data: 1 (expires in 5 minutes)
  - Buying power: 2 (expires in 1 minute)
  - Order params: 2 (expires in 30 seconds)
✓✓✓ Cache is ready for trading! ✓✓✓
```

## Step 4: Start Trading (When Market Opens)

### Option A: Headless Mode (Recommended for Production)

```bash
locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 30s
```

Parameters:
- `--users 10` - Run 10 concurrent traders
- `--spawn-rate 10` - Start 10 traders per second
- `--run-time 30s` - Run for 30 seconds

### Option B: Web Interface (For Monitoring)

```bash
locust -f locustfile_new.py
```

Then open: http://localhost:8089

Configure and click **Start Swarming**

## Cache Management

### View Cache Statistics

```bash
python cache_cli.py stats
```

### Clean Expired Entries

```bash
python cache_cli.py clean
```

### Clear All Cache

```bash
python cache_cli.py clear
```

### Clear Specific Cache Type

```bash
python cache_cli.py clear tokens      # Clear only tokens
python cache_cli.py clear market      # Clear only market data
python cache_cli.py clear buying      # Clear only buying power
python cache_cli.py clear orders      # Clear only order params
```

## Daily Workflow

### Morning Routine (Before Market Opens - 8:20 AM)

```bash
# 1. Clean old cache
python cache_cli.py clean

# 2. Warm up cache with fresh data
python cache_warmup.py

# 3. Verify cache is ready
python cache_cli.py stats
```

### Market Opens (8:30 AM)

```bash
# Start trading immediately with cached data
locust -f locustfile_new.py --headless --users 10 --spawn-rate 10 --run-time 30s
```

### After Trading

```bash
# View cache statistics
python cache_cli.py stats

# Clean expired entries
python cache_cli.py clean

# Check order results in order_results.json
```

## Common Issues

### Cache warmup fails with 429 error

**Solution:** The broker's API is rate-limiting. Wait a few minutes and retry.

### Authentication fails

**Solution:**
1. Verify credentials in `config.ini`
2. Clear cache: `python cache_cli.py clear`
3. Retry: `python cache_warmup.py`

### Orders not being placed

**Solution:**
1. Check buying power is sufficient
2. Verify ISIN code is correct
3. Ensure market is open
4. Check Locust logs for errors

## Performance Tips

### For Maximum Speed (Queue Bombing)

```bash
locust -f locustfile_new.py --headless --users 20 --spawn-rate 10 --run-time 30s
```

### For Sustained Trading

```bash
locust -f locustfile_new.py --headless --users 5 --spawn-rate 1 --run-time 5m
```

### For Testing

```bash
locust -f locustfile_new.py --headless --users 1 --spawn-rate 1 --run-time 1m
```

## Cache Expiry Times

- **Tokens:** 1 hour
- **Market Data:** 5 minutes  
- **Buying Power:** 1 minute
- **Order Params:** 30 seconds

These times are optimized for trading and don't need to be changed.

## Next Steps

- 📖 Read [README.md](../README.md) for feature overview
- 🔒 Review [SECURITY.md](../SECURITY.md) for security best practices
- 🗂️ Check [CACHING_IMPLEMENTATION.md](../CACHING_IMPLEMENTATION.md) for technical details

## Need Help?

1. Check **Common Issues** section above
2. Review error messages in terminal
3. Check `cache_warmup.log` for cache issues
4. Verify configuration in `config.ini`

---

**Happy trading! 🚀**

