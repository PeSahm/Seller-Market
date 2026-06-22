# captcha_utils.py
import os
import requests
import logging

import runtime_config

logger = logging.getLogger(__name__)

# OCR service URL(s) - uses environment variable for Docker compatibility.
# May hold a SINGLE URL or a comma/space-separated LIST of endpoints. The
# decoder tries them in order and fails over to the next on a transport error,
# so one OCR host going down doesn't stop captcha solving fleet-wide
# (client-side OCR pool — see the HA plan, WS1).
OCR_SERVICE_URL = os.getenv('OCR_SERVICE_URL', 'http://localhost:8080')

# Per-endpoint HTTP timeout (seconds). Unchanged from the original single-call
# value so failover composes with the caller's retry loops without multiplying.
_OCR_TIMEOUT_S = 10


def _ocr_base_urls():
    """Parse the OCR endpoint pool into an ordered list of base URLs.

    Precedence: the DB-pushed ``[runtime] ocr_service_url`` (changeable
    fleet-wide with no redeploy) wins; otherwise the ``OCR_SERVICE_URL`` env
    constant (baked into compose). Accepts a single URL or a comma/space-
    separated list; trailing slashes are stripped. A single URL yields a
    one-element list (backward compatible).
    """
    raw = runtime_config.get("ocr_service_url", "") or OCR_SERVICE_URL
    raw = (raw or '').replace(',', ' ')
    return [part.rstrip('/') for part in raw.split() if part.strip()]


def decode_captcha(im: str) -> str:
    """
    Decode a captcha image using the OCR service pool.

    Tries each configured OCR endpoint in order, failing over to the next ONLY
    on a transport/HTTP error. An empty-but-successful decode (the image was
    ambiguous, not the host's fault) returns ``""`` immediately so the caller
    re-fetches a fresh captcha instead of fanning one bad image across every
    host (the callers already retry captcha solving up to ~100x).

    Args:
        im: Base64 encoded image

    Returns:
        Decoded captcha text, or ``""`` if the decode was empty or every
        endpoint failed.
    """
    headers = {
        'accept': 'text/plain',
        'Content-Type': 'application/json',
    }
    data = {"base64": im}

    endpoints = _ocr_base_urls()
    if not endpoints:
        logger.error("No OCR endpoints configured (OCR_SERVICE_URL is empty)")
        return ""

    last_error = None
    for base in endpoints:
        url = f'{base}/ocr/captcha-easy-base64'
        try:
            response = requests.post(url, headers=headers, json=data, timeout=_OCR_TIMEOUT_S)
            response.raise_for_status()
        except requests.RequestException as e:
            last_error = e
            logger.warning(f"OCR endpoint {base} failed, trying next: {e}")
            continue

        result = response.text.strip()
        # Remove quotes if the response includes them
        if result.startswith('"') and result.endswith('"'):
            result = result[1:-1]
        if result:
            logger.debug(f"Captcha decoded via {base}: {result}")
            return result
        # Empty decode from a HEALTHY host = ambiguous image, not a host
        # failure. Don't burn the other endpoints on the same bad image; let
        # the caller re-fetch a fresh captcha.
        logger.debug(f"OCR endpoint {base} returned empty (ambiguous image)")
        return ""

    logger.error(f"All {len(endpoints)} OCR endpoint(s) failed: {last_error}")
    return ""
