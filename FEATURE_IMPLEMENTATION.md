# New Feature Implementation - Dynamic Order Calculation

## Overview
This update transforms the trading bot from a manual configuration system to a fully automated, intelligent order placement system.

## Key Improvements

### 1. **Simplified Configuration** ✨
**Before:**
```ini
[Order_Account_Broker]
username = 4580090306
password = Mm@12345
captcha = https://identity-gs.ephoenix.ir/api/Captcha/GetCaptcha 
login = https://identity-gs.ephoenix.ir/api/v2/accounts/login
order = https://api-gs.ephoenix.ir/api/v2/orders/NewOrder
editorder = https://api-gs.ephoenix.ir/api/v2/orders/EditOrder
validity = 1
side = 1
accounttype = 1
price = 5860          # Manual entry required daily
volume = 170017       # Manual entry required daily
isin = IRO1MHRN0001
serialnumber = 0
```

**After:**
```ini
[Order_Account_Broker]
username = 4580090306
password = Mm@12345
broker = gs
isin = IRO1MHRN0001
side = 1
```

### 2. **Automatic Price Detection** 🎯
- Fetches real-time market data from ephoenix.ir
- **Buy orders**: Uses `maxap` (maximum allowed price)
- **Sell orders**: Uses `minap` (minimum allowed price)
- No manual price updates needed!

### 3. **Dynamic Volume Calculation** 📊
The bot now automatically:
1. Fetches your current **buying power**
2. Calls the **CalculateOrderParam** API
3. Compares with **maxeq** (maximum allowed volume)
4. Uses the smaller value to ensure compliance

### 4. **Broker Enum System** 🏦
New `broker_enum.py` provides:
```python
class BrokerCode(Enum):
    GANJINE = "gs"
    SHAHR = "shahr"
    BOURSE_BIME = "bbi"
    KARAMAD = "karamad"
    TEJARAT = "tejarat"
    SHAMS = "shams"
```

All endpoints automatically generated:
```python
endpoints = BrokerCode.GANJINE.get_endpoints()
# Returns captcha, login, order, trading_book, etc.
```

### 5. **Comprehensive Logging** 📝
Enhanced logging throughout the application:
```
2025-11-05 10:00:00 - INFO - ================================================================================
2025-11-05 10:00:00 - INFO - Preparing order for 4580090306@gs - ISIN: IRO1MHRN0001
2025-11-05 10:00:00 - INFO - ================================================================================
2025-11-05 10:00:00 - INFO - Step 1: Authenticating...
2025-11-05 10:00:00 - INFO - ✓ Authentication successful
2025-11-05 10:00:00 - INFO - Step 2: Fetching buying power...
2025-11-05 10:00:00 - INFO - ✓ Buying power: 1,000,014,598 Rials
2025-11-05 10:00:00 - INFO - Step 3: Fetching instrument information...
2025-11-05 10:00:00 - INFO - ✓ Instrument: بورس اوراق بهادار تهران (بورس)
2025-11-05 10:00:00 - INFO - ✓ Buy order - Using max price: 3,601
2025-11-05 10:00:00 - INFO - Step 4: Calculating order volume...
2025-11-05 10:00:00 - INFO - ✓ Calculated volume: 276,677 shares
2025-11-05 10:00:00 - INFO - Step 5: Preparing order payload...
2025-11-05 10:00:00 - INFO - ✓ Order prepared:
2025-11-05 10:00:00 - INFO -   ISIN: IRO1MHRN0001
2025-11-05 10:00:00 - INFO -   Side: Buy
2025-11-05 10:00:00 - INFO -   Price: 3,601 Rials
2025-11-05 10:00:00 - INFO -   Volume: 276,677 shares
2025-11-05 10:00:00 - INFO -   Total: 996,410,677 Rials
```

Logs saved to `trading_bot.log` file.

### 6. **Order Result Tracking** 📋
New feature: Automatic order result collection when test stops!

**On test completion:**
1. Calls `GetOpenOrders` API for each user
2. Saves results to `order_results/` directory
3. Generates summary reports

**Example output:**
```
======================================================================
Order Summary: 4580090306@gs
======================================================================
Total Orders: 3
Total Volume: 830,031 shares
Executed Volume: 415,000 shares (50.0%)
Total Amount: 2,989,231,677 Rials

Order Details:
----------------------------------------------------------------------
1404/08/15 | بورس     | Buy  |  276,677 @ 3,601 | Partially Filled
1404/08/15 | مهرگان   | Buy  |  276,677 @ 5,860 | Fully Executed
1404/08/15 | سیمان    | Buy  |  276,677 @ 4,200 | Pending
======================================================================
```

Files saved as: `4580090306_gs_20251105_143022.json`

### 7. **Modular Architecture** 🏗️
New file structure:
```
SellerMarket/
├── locustfile_new.py       # Main application
├── broker_enum.py           # Broker enumeration
├── api_client.py            # API client class
├── order_tracker.py         # Order result tracking
├── test_trading_bot.py      # Unit tests
├── requirements.txt         # Dependencies
├── config.simple.example.ini # Config template
├── trading_bot.log          # Application logs
└── order_results/           # Order results directory
    └── {username}_{broker}_{timestamp}.json
```

### 8. **Unit Tests** ✅
Comprehensive test coverage:
```bash
python -m unittest test_trading_bot.py
```

Tests include:
- ✅ Broker enum validation
- ✅ API client authentication
- ✅ Buying power fetching
- ✅ Instrument info retrieval
- ✅ Volume calculation
- ✅ Order result tracking
- ✅ End-to-end flow simulation

### 9. **API Flow** 🔄

```
┌─────────────────────────────────────────────────────────────────┐
│                    Start Trading Bot                             │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step 1: Authentication                                          │
│  ├── Fetch captcha                                              │
│  ├── Decode with OCR                                            │
│  ├── Login with credentials                                     │
│  └── Cache JWT token (2 hours)                                  │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step 2: Get Buying Power                                        │
│  GET /api/v2/tradingbook/GetLastTradingBook                     │
│  Response: { "buyingPower": 1000014598, ... }                   │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step 3: Get Instrument Info                                     │
│  POST /api/v2/instruments/full                                   │
│  Body: { "isinList": ["IRO1MHRN0001"] }                         │
│  Response:                                                       │
│    ├── t.maxap (max price for buy)                             │
│    ├── t.minap (min price for sell)                            │
│    └── i.maxeq (max allowed volume)                            │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step 4: Calculate Volume                                        │
│  POST /api/v2/orders/CalculateOrderParam                        │
│  Body: {                                                         │
│    "isin": "IRO1MHRN0001",                                      │
│    "side": 1,                                                    │
│    "totalNetAmount": 1000014598,                                │
│    "price": 3601                                                │
│  }                                                               │
│  Response: { "volume": 276677, ... }                            │
│  Final Volume: min(calculated, maxeq)                           │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step 5: Place Order                                             │
│  POST /api/v2/orders/NewOrder                                    │
│  Body: {                                                         │
│    "isin": "IRO1MHRN0001",                                      │
│    "side": 1,                                                    │
│    "validity": 1,                                               │
│    "accountType": 1,                                            │
│    "price": 3601,                                               │
│    "volume": 276677,                                            │
│    "validityDate": null,                                        │
│    "serialNumber": 0                                            │
│  }                                                               │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  On Test Stop: Get Order Results                                │
│  GET /api/v2/orders/GetOpenOrders?type=1                        │
│  ├── Save to order_results/ directory                           │
│  ├── Generate summary report                                    │
│  └── Log results                                                │
└─────────────────────────────────────────────────────────────────┘
```

## Migration Guide

### Quick Migration
1. **Backup old config:**
   ```bash
   cp config.ini config.ini.backup
   ```

2. **Create new config:**
   ```bash
   cp config.simple.example.ini config.ini
   ```

3. **Edit config.ini:**
   ```ini
   [Order_MyAccount_GS]
   username = YOUR_USERNAME
   password = YOUR_PASSWORD
   broker = gs
   isin = IRO1MHRN0001
   side = 1
   ```

4. **Use new locustfile:**
   ```bash
   locust -f locustfile_new.py
   ```

### Running Tests
```bash
# Install dependencies
pip install -r requirements.txt

# Run unit tests
python -m unittest test_trading_bot.py -v

# Run with coverage (optional)
pip install coverage
coverage run -m unittest test_trading_bot.py
coverage report
```

### Checking Logs
```bash
# Real-time log monitoring
tail -f trading_bot.log

# Search for errors
grep ERROR trading_bot.log

# View specific user
grep "4580090306@gs" trading_bot.log
```

### Viewing Order Results
```bash
# List all results
ls -la order_results/

# View latest results
cat order_results/$(ls -t order_results/ | head -1)

# Pretty print JSON
cat order_results/*.json | python -m json.tool
```

## Benefits Summary

| Feature | Before | After |
|---------|--------|-------|
| **Config Lines** | 14 lines | 5 lines |
| **Manual Updates** | Daily (price & volume) | Never |
| **API Calls** | 1 (order) | 5 (auth, buying power, instrument, calculate, order) |
| **Logging** | Minimal | Comprehensive |
| **Order Tracking** | Manual | Automatic |
| **Volume Safety** | Manual check | Auto-constrained |
| **Price Accuracy** | User input | Market data |
| **Testing** | None | Full unit tests |

## Breaking Changes

⚠️ **Important**: The new version uses `locustfile_new.py` and requires a different config format.

**Old locustfile.py still works** with the old config format if you need backward compatibility.

## Next Steps

1. ✅ Migrate configuration
2. ✅ Run unit tests
3. ✅ Test with small volumes first
4. ✅ Monitor logs for any issues
5. ✅ Check order results after test
6. ✅ Gradually increase to production volumes

## Support

- **Logs**: Check `trading_bot.log` for detailed execution logs
- **Results**: Review `order_results/` for order outcomes
- **Tests**: Run `test_trading_bot.py` to validate functionality

---

**Last Updated**: November 6, 2025  
**Version**: 2.0.0  
**Status**: Ready for Testing
