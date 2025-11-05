# Quick Start Guide

## Prerequisites

### Required Software
- Python 3.8 or higher
- pip (Python package manager)
- OCR Service running on localhost:8080

### Required Python Packages
```bash
pip install locust requests python-dotenv configparser
```

## Initial Setup

### 1. Clone and Setup Repository

```bash
# Clone the repository
git clone https://github.com/MostafaEsmaeili/Seller-Market.git
cd Seller-Market/SellerMarket

# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install locust requests
```

### 2. Configure Trading Accounts

```bash
# Copy example configuration
cp config.example.ini config.ini

# Edit config.ini with your credentials
# IMPORTANT: Never commit this file to git!
```

### 3. Setup OCR Service

The application requires a CAPTCHA solving service:

```bash
# Option 1: Use provided OCR service at localhost:8080
# (Service must support /ocr/by-base64 endpoint)

# Option 2: Modify the OCR URL in locustfile.py
# Change line 38 from:
# url = 'http://localhost:8080/ocr/by-base64'
# to your OCR service URL
```

## Configuration Guide

### Config.ini Structure

```ini
[SectionName_AccountName_BrokerCode]
username = YOUR_ACCOUNT_NUMBER
password = YOUR_PASSWORD
captcha = https://identity-BROKER.ephoenix.ir/api/Captcha/GetCaptcha
login = https://identity-BROKER.ephoenix.ir/api/v2/accounts/login
order = https://api-BROKER.ephoenix.ir/api/v2/orders/NewOrder
editorder = https://api-BROKER.ephoenix.ir/api/v2/orders/EditOrder
validity = 1
side = 1
accounttype = 1
price = DESIRED_PRICE
volume = NUMBER_OF_SHARES
isin = STOCK_ISIN_CODE
serialnumber = 0
```

### Parameter Explanations

| Parameter | Values | Description |
|-----------|--------|-------------|
| `validity` | 1, 2 | 1=Day order, 2=GTC (Good Till Cancel) |
| `side` | 1, 2 | 1=Buy, 2=Sell |
| `accounttype` | 1 | 1=Default account type |
| `price` | Integer | Order price in Rials |
| `volume` | Integer | Number of shares to trade |
| `isin` | String | Stock identifier (e.g., IRO1MHRN0001) |
| `serialnumber` | 0 or ID | 0=New order, >0=Edit existing order |

### Supported Brokers

Replace `BROKER` in URLs with one of:
- `gs` - Ghadir Shahr
- `bbi` - Bourse Bazar Iran
- `shahr` - Shahr
- `karamad` - Karamad
- `tejarat` - Tejarat

## Running the Application

### Method 1: Locust Web UI (Recommended)

```bash
# Start Locust with web interface
locust -f locustfile.py

# Open browser and navigate to:
# http://localhost:8089

# Configure test:
# - Number of users: How many concurrent traders
# - Spawn rate: Users spawned per second
# - Host: Leave empty (configured in code)

# Click "Start Swarming"
```

### Method 2: Command Line (Headless)

```bash
# Run without web UI
locust -f locustfile.py --headless --users 10 --spawn-rate 2 --run-time 1m

# Parameters:
# --users: Number of concurrent users
# --spawn-rate: Users spawned per second
# --run-time: Test duration (e.g., 1m, 30s, 2h)
```

### Method 3: Single User Test

```bash
# Test with single user (debugging)
python locustfile.py
```

## Monitoring and Logs

### Real-Time Monitoring

When using Locust web UI, you can monitor:
- **Statistics**: Request counts, response times, failures
- **Charts**: Real-time performance graphs
- **Failures**: Error messages and stack traces
- **Exceptions**: Detailed exception logs

### Token Files

Authentication tokens are cached in the project directory:
```
4580090306_identity_gs_ephoenix_ir.txt
```

Format:
```
<TOKEN_STRING>
<TIMESTAMP>
```

Tokens are valid for 2 hours and automatically refreshed.

### Console Output

```bash
# Successful login
4580090306
captcha is 12345
login ok ! 4580090306 https://identity-gs.ephoenix.ir/api/v2/accounts/login
Section: Order_Mostafa_Mehregan_GS

# Request logs
POST /api/v2/orders/NewOrder
Response: 200 OK
```

## Troubleshooting

### Common Issues

#### 1. OCR Service Not Running
```
Error: Connection refused to localhost:8080
```
**Solution**: Start your OCR service before running the script

#### 2. Invalid Captcha
```
captcha is 
Error: Invalid captcha
```
**Solution**: Check OCR service accuracy, may need multiple retries

#### 3. Authentication Failed
```
Error: 401 Unauthorized
```
**Solution**: Verify username/password in config.ini

#### 4. Order Rejected
```
Error: 400 Bad Request
```
**Solution**: Check price limits, account balance, market hours

#### 5. Token Expired
```
Error: 401 Unauthorized (after initial success)
```
**Solution**: Delete token files and restart (auto-regenerated)

### Debug Mode

Enable verbose logging:

```python
# Add to locustfile.py
import logging
logging.basicConfig(level=logging.DEBUG)
```

## Testing Strategy

### Phase 1: Single Account Test
1. Configure ONE account in config.ini
2. Set low volume (e.g., 1 share)
3. Run with 1 user
4. Verify order placement

### Phase 2: Multi-Account Test
1. Add 2-3 accounts
2. Increase volume gradually
3. Run with 3-5 users
4. Monitor success rate

### Phase 3: Load Test
1. Configure all accounts
2. Set production volumes
3. Run with 10+ users
4. Monitor broker API responses

### Phase 4: Production
1. Verify all orders successful in Phase 3
2. Set real prices and volumes
3. Monitor continuously during market hours

## Performance Optimization

### Recommended Settings

**For Queue Bombing (High Speed):**
```bash
locust -f locustfile.py --headless --users 20 --spawn-rate 10 --run-time 30s
```

**For Sustained Trading (Moderate Speed):**
```bash
locust -f locustfile.py --headless --users 5 --spawn-rate 1 --run-time 5m
```

**For Testing (Low Speed):**
```bash
locust -f locustfile.py --headless --users 1 --spawn-rate 1 --run-time 1m
```

### System Requirements

- **CPU**: 2+ cores recommended
- **RAM**: 2GB+ available
- **Network**: Stable connection, low latency to broker APIs
- **Disk**: Minimal (token files are small)

## Safety Features

### Rate Limiting
Currently not implemented - Consider adding:

```python
from time import sleep
sleep(0.1)  # 100ms delay between requests
```

### Kill Switch
Stop all tests immediately:
- **Web UI**: Click "Stop" button
- **Command Line**: Press `Ctrl+C`
- **Emergency**: Kill Python process

## Next Steps

1. ✅ Complete initial setup
2. ✅ Test with paper trading account
3. ✅ Verify order placement
4. ✅ Read SECURITY.md thoroughly
5. ✅ Consult legal advisor
6. ✅ Obtain broker approval
7. ✅ Start with small volumes
8. ✅ Monitor continuously

## Support

For issues or questions:
- Check SECURITY.md for security concerns
- Review README_DETAILED.md for architecture details
- Contact broker support for trading issues
- Consult legal advisor for compliance questions

---

**Remember**: Start small, test thoroughly, and always comply with regulations!
