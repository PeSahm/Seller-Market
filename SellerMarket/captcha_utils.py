# captcha_utils.py
import requests
import logging

logger = logging.getLogger(__name__)

def decode_captcha(im: str) -> str:
    """
    Decode captcha image using OCR service.
    
    Args:
        im: Base64 encoded image
        
    Returns:
        Decoded captcha text
    """
    url = 'http://localhost:8080/ocr/captcha-easy-base64'
    headers = {
        'accept': 'text/plain',
        'Content-Type': 'application/json'
    }
    data = {"base64": im}
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        response.raise_for_status()
        result = response.text.strip()
        # Remove quotes if the response includes them
        if result.startswith('"') and result.endswith('"'):
            result = result[1:-1]
        logger.debug(f"Captcha decoded: {result}")
        return result
    except requests.RequestException as e:
        logger.error(f"Captcha decoding failed: {e}")
        return ""