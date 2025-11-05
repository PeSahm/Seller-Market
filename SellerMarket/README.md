# SellerMarket - Trading Bot

This directory contains the automated stock trading bot for Iranian stock exchanges.

## 🚀 Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Setup Configuration
```bash
cp config.simple.example.ini config.ini
# Edit config.ini with your account details
```

### 3. Run the Bot
```bash
# New version with dynamic calculation
locust -f locustfile_new.py

# Old version (backward compatibility)
locust -f locustfile.py
```

### 4. Run Tests
```bash
python -m unittest test_trading_bot.py -v
```

## 📁 File Structure

### Core Application Files
- **`locustfile_new.py`** - Main application with dynamic order calculation
- **`locustfile.py`** - Legacy version (backward compatible)
- **`broker_enum.py`** - Broker codes and endpoint management
- **`api_client.py`** - API client for ephoenix.ir platform
- **`order_tracker.py`** - Order result tracking and reporting

### Configuration Files
- **`config.ini`** - Your trading configuration (DO NOT COMMIT)
- **`config.simple.example.ini`** - Simple config template
- **`config.example.ini`** - Full config template (legacy)

### Testing & Dependencies
- **`test_trading_bot.py`** - Unit tests
- **`requirements.txt`** - Python dependencies

### Output Directories
- **`order_results/`** - Order execution results (auto-created)
- **`trading_bot.log`** - Application logs

## 🎯 New vs Old Version

### New Version (`locustfile_new.py`)
✅ **5-line configuration**  
✅ **Automatic price from market data**  
✅ **Automatic volume from buying power**  
✅ **Comprehensive logging**  
✅ **Automatic order tracking**  
✅ **Full unit tests**

**Configuration:**
```ini
[Order_Account_Broker]
username = YOUR_USERNAME
password = YOUR_PASSWORD
broker = gs
isin = IRO1MHRN0001
side = 1
```

### Old Version (`locustfile.py`)
⚠️ **14-line configuration**  
⚠️ **Manual price entry required daily**  
⚠️ **Manual volume entry required daily**  
⚠️ **Minimal logging**  
⚠️ **Manual order checking**

**Configuration:**
```ini
[Order_Account_Broker]
username = YOUR_USERNAME
password = YOUR_PASSWORD
captcha = https://identity-gs.ephoenix.ir/api/Captcha/GetCaptcha
login = https://identity-gs.ephoenix.ir/api/v2/accounts/login
order = https://api-gs.ephoenix.ir/api/v2/orders/NewOrder
editorder = https://api-gs.ephoenix.ir/api/v2/orders/EditOrder
validity = 1
side = 1
accounttype = 1
price = 5860       # Manual entry
volume = 170017    # Manual entry
isin = IRO1MHRN0001
serialnumber = 0
```

## 🔧 Configuration Options

### Broker Codes
- `gs` - Ganjine (Ghadir Shahr)
- `bbi` - Bourse Bazar Iran
- `shahr` - Shahr
- `karamad` - Karamad
- `tejarat` - Tejarat
- `shams` - Shams

### Order Sides
- `1` - Buy order
- `2` - Sell order

### Auto-Configured Values
- `validity = 1` - Day order
- `accounttype = 1` - Default account
- `serialnumber = 0` - New order

## 📊 Monitoring

### Real-time Logs
```bash
tail -f trading_bot.log
```

### Order Results
```bash
ls -la order_results/
cat order_results/*.json
```

### Locust Web UI
Open http://localhost:8089 after starting locust

## 🧪 Testing

### Run All Tests
```bash
python -m unittest test_trading_bot.py
```

### Run Specific Test
```bash
python -m unittest test_trading_bot.TestAPIClient.test_get_buying_power
```

### Run with Verbose Output
```bash
python -m unittest test_trading_bot.py -v
```

## 🔒 Security

**IMPORTANT:**
- Never commit `config.ini` to git
- Change exposed passwords immediately
- Review `../SECURITY.md` for details

## 📚 Documentation

- **../IMPLEMENTATION_SUMMARY.md** - What was implemented
- **../FEATURE_IMPLEMENTATION.md** - Feature details and migration guide
- **../QUICKSTART.md** - Complete setup guide
- **../SECURITY.md** - Security warnings and best practices
- **../README_DETAILED.md** - Technical architecture

## 🐛 Troubleshooting

### OCR Service Not Running
```
Error: Connection refused to localhost:8080
```
**Solution:** Start your OCR service before running

### Authentication Failed
```
Error: 401 Unauthorized
```
**Solution:** Check username/password in config.ini

### Order Rejected
```
Error: 400 Bad Request
```
**Solution:** Check market hours, price limits, account balance

### Import Errors
```
ModuleNotFoundError: No module named 'locust'
```
**Solution:** `pip install -r requirements.txt`

## 📞 Support

- Check `trading_bot.log` for detailed errors
- Review test output: `python -m unittest test_trading_bot.py`
- See parent directory documentation for comprehensive guides

---

**Version:** 2.0.0  
**Last Updated:** November 6, 2025  
**Python:** 3.8+
