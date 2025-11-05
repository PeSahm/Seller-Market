# SECURITY AND LEGAL NOTICE

## ⚠️ CRITICAL SECURITY WARNINGS

### Immediate Actions Required

1. **REMOVE SENSITIVE DATA FROM GIT HISTORY**
   ```bash
   # Your config.ini and token files are currently tracked in git!
   # They contain plaintext passwords and authentication tokens
   
   # Remove from future commits (already added to .gitignore)
   git rm --cached SellerMarket/config.ini
   git rm --cached SellerMarket/*_identity_*.txt
   git rm --cached SellerMarket/*_ephoenix_*.txt
   
   # Commit the removal
   git commit -m "Remove sensitive configuration and token files"
   
   # IMPORTANT: These files are still in your git history!
   # Consider using git-filter-repo or BFG Repo-Cleaner to remove them permanently
   ```

2. **CHANGE ALL EXPOSED PASSWORDS IMMEDIATELY**
   - Account: 4580090306 (password exposed: Mm@12345)
   - Account: 0780203674 (password exposed: Mm@12345)
   - Account: 5219466356 (password exposed: Mm@12345)
   
   **These passwords are now publicly visible on GitHub!**

3. **REVOKE ALL ACTIVE TOKENS**
   - Delete all `*_identity_*.txt` files
   - Log out from all broker sessions
   - Tokens in these files may still be valid for up to 2 hours

---

## 🔒 Security Best Practices

### Configuration Security

**Current Issues:**
- ❌ Plaintext passwords in config.ini
- ❌ No encryption for sensitive data
- ❌ Credentials committed to version control
- ❌ Token files stored unencrypted

**Recommended Solutions:**

#### Option 1: Environment Variables (Recommended)
```python
import os
from dotenv import load_dotenv

load_dotenv()

username = os.getenv('BROKER_USERNAME')
password = os.getenv('BROKER_PASSWORD')
```

Create `.env` file (already in .gitignore):
```bash
BROKER_USERNAME=your_username
BROKER_PASSWORD=your_password
BROKER_CAPTCHA_URL=https://identity-gs.ephoenix.ir/api/Captcha/GetCaptcha
```

#### Option 2: Encrypted Configuration
```python
from cryptography.fernet import Fernet
import configparser

def load_encrypted_config(key_file, config_file):
    with open(key_file, 'rb') as f:
        key = f.read()
    
    fernet = Fernet(key)
    
    with open(config_file, 'rb') as f:
        encrypted_data = f.read()
    
    decrypted_data = fernet.decrypt(encrypted_data)
    config = configparser.ConfigParser()
    config.read_string(decrypted_data.decode())
    
    return config
```

#### Option 3: System Keyring
```python
import keyring

# Store password securely
keyring.set_password("stock_trading", "username", "password")

# Retrieve password
password = keyring.get_password("stock_trading", "username")
```

---

## ⚖️ LEGAL AND COMPLIANCE WARNINGS

### Market Manipulation Risks

This application is designed for **"bombing" trades** - rapidly placing multiple orders to compete in buying queues. This practice may constitute:

1. **Market Manipulation** (Iranian Securities and Exchange Organization violations)
   - Artificial price inflation
   - Queue manipulation
   - Disruption of fair market practices

2. **Broker Terms of Service Violations**
   - Automated trading restrictions
   - API abuse
   - Rate limiting violations

3. **Potential Criminal Charges**
   - Market fraud
   - Unauthorized system access
   - Securities law violations

### Regulatory Framework

**Iranian Capital Market Regulations:**
- SEO (Securities and Exchange Organization) guidelines
- TSE (Tehran Stock Exchange) rules
- Broker-specific policies

**Compliance Requirements:**
- Proper authorization for automated trading
- Disclosure of algorithmic trading systems
- Risk management protocols
- Audit trail maintenance

### Recommended Actions

1. **Legal Review**: Consult with securities lawyer
2. **Broker Approval**: Obtain written permission for automated trading
3. **Rate Limiting**: Implement throttling to avoid API abuse
4. **Monitoring**: Log all trades for regulatory compliance
5. **Paper Trading**: Test with demo accounts first

---

## 🛡️ Technical Security Measures

### 1. Network Security

```python
# Add SSL certificate verification
import requests

response = requests.post(
    url,
    json=data,
    verify=True,  # Verify SSL certificates
    timeout=30    # Prevent hanging connections
)
```

### 2. Input Validation

```python
def validate_isin(isin):
    """Validate ISIN format"""
    if not re.match(r'^IRO[A-Z0-9]{9}$', isin):
        raise ValueError(f"Invalid ISIN format: {isin}")
    return isin

def validate_price(price):
    """Ensure price is within reasonable bounds"""
    if price <= 0 or price > 1000000:
        raise ValueError(f"Price out of range: {price}")
    return price
```

### 3. Rate Limiting

```python
from ratelimit import limits, sleep_and_retry

@sleep_and_retry
@limits(calls=10, period=60)  # 10 calls per minute
def place_order(order_data):
    """Rate-limited order placement"""
    return client.post(order_url, json=order_data)
```

### 4. Secure Token Storage

```python
import base64
from cryptography.fernet import Fernet
from datetime import datetime, timedelta

class SecureTokenManager:
    def __init__(self, key):
        self.fernet = Fernet(key)
    
    def save_token(self, username, token):
        """Encrypt and save token"""
        data = f"{token}|{datetime.now().isoformat()}"
        encrypted = self.fernet.encrypt(data.encode())
        
        with open(f".tokens/{username}.enc", 'wb') as f:
            f.write(encrypted)
    
    def load_token(self, username):
        """Load and decrypt token"""
        try:
            with open(f".tokens/{username}.enc", 'rb') as f:
                encrypted = f.read()
            
            decrypted = self.fernet.decrypt(encrypted).decode()
            token, timestamp = decrypted.split('|')
            token_time = datetime.fromisoformat(timestamp)
            
            if datetime.now() - token_time < timedelta(hours=2):
                return token
        except (FileNotFoundError, ValueError):
            return None
```

---

## 📋 Compliance Checklist

### Before Running in Production:

- [ ] Passwords changed from exposed credentials
- [ ] config.ini removed from git history
- [ ] Token files encrypted or removed
- [ ] Legal counsel consulted
- [ ] Broker approval obtained
- [ ] Rate limiting implemented
- [ ] Audit logging enabled
- [ ] SSL certificate verification enabled
- [ ] Input validation implemented
- [ ] Error handling enhanced
- [ ] Paper trading completed successfully
- [ ] Risk management protocols documented
- [ ] Incident response plan created

---

## 🚨 Incident Response

### If Credentials Are Compromised:

1. **Immediately change all passwords**
2. **Revoke all active sessions**
3. **Check account activity for unauthorized trades**
4. **Contact broker security team**
5. **Monitor account for suspicious activity**
6. **Consider freezing account temporarily**

### If Unauthorized Trades Occur:

1. **Document all trades (screenshots, logs)**
2. **Contact broker immediately**
3. **File formal complaint with SEO if needed**
4. **Preserve all evidence**
5. **Consult legal counsel**

---

## 📞 Support Contacts

### Broker Security Contacts:
- **GS (Ghadir Shahr)**: [Contact security team]
- **BBI**: [Contact security team]
- **Shahr**: [Contact security team]

### Regulatory Bodies:
- **SEO (Securities and Exchange Organization)**: www.seo.ir
- **TSE (Tehran Stock Exchange)**: www.tse.ir

---

## 📝 Disclaimer

**This software is provided for educational purposes only.** The authors and contributors:

- ❌ Do NOT encourage market manipulation
- ❌ Do NOT guarantee compliance with regulations
- ❌ Are NOT responsible for financial losses
- ❌ Are NOT responsible for legal consequences
- ❌ Do NOT provide investment advice
- ❌ Are NOT liable for security breaches

**USE AT YOUR OWN RISK**

Users must ensure compliance with all applicable laws, regulations, and terms of service. Consult professional legal and financial advisors before deploying automated trading systems.

---

**Last Updated**: November 5, 2025  
**Severity**: CRITICAL  
**Action Required**: IMMEDIATE
