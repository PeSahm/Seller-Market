# Stock Trading Load Testing Application

## Overview
This is a **Locust-based load testing application** designed for automated stock trading on Iranian stock exchanges (ephoenix.ir platforms). The application performs high-volume order placement to test trading systems under load or to rapidly execute orders for stocks in buying queues.

## Architecture

### Core Components

#### 1. **Configuration System (`config.ini`)**
- Supports multiple trading accounts and brokers
- Each section represents a unique user/broker combination
- Configuration parameters:
  - `username`: Trading account identifier
  - `password`: Account password
  - `captcha`: Captcha generation endpoint
  - `login`: Authentication endpoint
  - `order`: New order placement endpoint
  - `editorder`: Order modification endpoint
  - `validity`: Order validity type (1 = Day order)
  - `side`: Trade side (1 = Buy, 2 = Sell)
  - `accounttype`: Account type (1 = Default account)
  - `price`: Order price in Rials
  - `volume`: Number of shares to trade
  - `isin`: Stock identifier (International Securities Identification Number)
  - `serialnumber`: Order ID for editing (0 = new order)

#### 2. **Authentication System**
- **Token Caching**: Saves authentication tokens to local files to avoid repeated logins
- **Token Format**: `{username}_{domain}.txt` contains token and timestamp
- **Token Expiry**: 2-hour validity window
- **Captcha Solving**: Integrates with external OCR service at `http://localhost:8080/ocr/by-base64`

#### 3. **Load Testing Framework (Locust)**
- Dynamically creates HttpUser classes from configuration
- Each config section becomes a separate user class
- Concurrent execution of multiple trading accounts
- Supports both new orders and order modifications

## Key Features

### 1. **Multi-Broker Support**
Currently configured brokers:
- **GS (Ghadir Shahr)**: identity-gs.ephoenix.ir / api-gs.ephoenix.ir
- **BBI (Bourse Bazar Iran)**: identity-bbi.ephoenix.ir / api-bbi.ephoenix.ir
- **Shahr**: identity-shahr.ephoenix.ir / api-shahr.ephoenix.ir
- Commented configurations for: Karamad, Tejarat, Shams

### 2. **Captcha Bypass**
- Automatic captcha solving using OCR service
- Retry mechanism for failed captcha attempts
- Persistent login until successful authentication

### 3. **Token Management**
- **Persistent Storage**: Tokens saved to disk with timestamps
- **Auto-Refresh**: Expired tokens automatically regenerated
- **Format**: `{username}_{normalized_domain}.txt`
- **Reduces API Load**: Minimizes unnecessary login requests

### 4. **Order Execution Strategy**
- **New Orders**: For stocks with `serialnumber = 0`
- **Edit Orders**: For existing orders with `serialnumber > 0`
- **Rapid Execution**: Designed for high-frequency order placement
- **Queue Bombing**: Ideal for competing in buying queues

## Use Cases

### Primary Use Case: **Queue Bombing**
When stocks have high demand and buying queues:
1. Multiple accounts configured with same ISIN
2. Simultaneous order placement across brokers
3. Increases probability of order execution
4. Competes with other buyers in the queue

### Secondary Use Cases:
- **Load Testing**: Stress testing broker APIs
- **Performance Benchmarking**: Measuring order placement latency
- **Multi-Account Management**: Coordinated trading across accounts

## Technical Implementation

### Dynamic Class Generation
```python
for section_name in config.sections():
    section = dict(config[section_name])
    data = on_locust_init(section)
    globals()[section_name] = type(section_name, (Mostafa_Ib,), {})
    globals()[section_name].Populate(globals()[section_name], data.data, data.order, data.token)
```

### Authentication Flow
1. Check for cached token (< 2 hours old)
2. If expired/missing: Fetch captcha → Decode → Login → Cache token
3. Retry login on captcha failure
4. Return token + order endpoint

### Order Placement
- **Method**: HTTP POST
- **Authentication**: Bearer token
- **Content-Type**: application/json
- **Payload**: JSON-serialized order parameters
- **Headers**: Mimics browser request (Chrome User-Agent)

## Configuration Examples

### Active Configuration (Mehregān Stock - IRO1MHRN0001)
```ini
[Order_Mostafa_Mehregan_GS]
username = 4580090306
password = Mm@12345
price = 5860
volume = 170017
isin = IRO1MHRN0001
side = 1  # Buy order
```

### Commented Configurations
Multiple alternative brokers and stocks are configured but commented out for selective activation.

## Dependencies

### Required Services:
1. **OCR Service**: `http://localhost:8080/ocr/by-base64` (must be running)
2. **Broker APIs**: ephoenix.ir platform endpoints
3. **Python Packages**:
   - `locust`: Load testing framework
   - `requests`: HTTP client
   - `configparser`: Configuration parsing

## Security Considerations

### ⚠️ Critical Issues:
1. **Plaintext Passwords**: Credentials stored in plaintext in `config.ini`
2. **Public Repository**: Sensitive data exposed in version control
3. **Hardcoded Credentials**: No environment variable support
4. **No Encryption**: Token files stored unencrypted

### Recommendations:
- **Immediate**: Add `config.ini` and `*.txt` token files to `.gitignore`
- **Migrate**: Use environment variables or encrypted key stores
- **Implement**: Configuration encryption for production use
- **Add**: `.env.example` file with placeholder values

## Performance Optimization

### Current Optimizations:
- Token caching reduces login API calls
- Retry logic ensures successful authentication
- Concurrent user execution via Locust

### Potential Improvements:
- Connection pooling for HTTP requests
- Async request handling
- Configurable retry strategies
- Rate limiting controls

## Running the Application

### Prerequisites:
```bash
# Install dependencies
pip install locust requests

# Start OCR service
# (External service must be running on localhost:8080)
```

### Execution:
```bash
# Run Locust load test
locust -f locustfile.py

# Access web UI at http://localhost:8089
```

### Configuration:
1. Edit `config.ini` with target accounts
2. Set desired price, volume, and ISIN
3. Uncomment sections to activate multiple accounts
4. Ensure `serialnumber = 0` for new orders

## Legal and Ethical Considerations

### ⚠️ Important Warnings:
1. **Market Manipulation**: High-frequency order bombing may violate securities regulations
2. **Broker ToS**: Automated trading may breach broker terms of service
3. **Fair Trading**: Queue manipulation disrupts market fairness
4. **Legal Risk**: Potential regulatory action from securities commission

### Compliance Requirements:
- Review Iranian Securities and Exchange Organization (SEO) regulations
- Verify broker API usage terms
- Consider rate limits and fair use policies
- Implement proper risk management

## Troubleshooting

### Common Issues:
1. **Captcha Failures**: Ensure OCR service is running and accurate
2. **Token Expiry**: Check token file timestamps
3. **Connection Errors**: Verify broker API availability
4. **Order Rejection**: Validate price limits and account balance

### Debug Mode:
- Enable verbose logging in Locust
- Monitor token file creation/updates
- Check network requests in browser DevTools

## Future Enhancements

### Planned Features:
- [ ] Real-time market data integration
- [ ] Price adjustment algorithms
- [ ] Order status monitoring
- [ ] Multi-stage order strategies
- [ ] Configurable retry policies
- [ ] Secure credential management
- [ ] Comprehensive error handling
- [ ] Performance metrics dashboard

## File Structure
```
SellerMarket/
├── locustfile.py          # Main load testing script
├── config.ini             # Trading configuration
├── *.txt                  # Cached authentication tokens
└── README_DETAILED.md     # This documentation
```

## Contributing
When contributing to this project:
1. Never commit credentials or tokens
2. Test with paper trading accounts first
3. Follow PEP 8 Python style guidelines
4. Document configuration changes
5. Consider legal implications of changes

## Disclaimer
This software is provided for educational and testing purposes only. Users are solely responsible for compliance with applicable laws and regulations. The authors assume no liability for misuse, market manipulation, or regulatory violations resulting from use of this software.

---

**Last Updated**: November 5, 2025  
**Version**: 1.0  
**License**: See LICENSE file
