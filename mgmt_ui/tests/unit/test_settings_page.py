"""Unit tests for the admin Settings form schema (``app.schemas.settings_page``).

Focus: the ``ocr_service_url`` validator now accepts a comma/space-separated
LIST of endpoints (client-side OCR pool — HA plan WS1) while keeping a single
URL byte-identical.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.settings_page import SettingsUpdate


def _base(**over):
    data = {
        "ocr_service_url": "http://5.10.248.55:18080",
        "agent_image_tag": "ghcr.io/pesahm/seller-market:latest",
    }
    data.update(over)
    return data


def test_single_ocr_url_roundtrips_unchanged():
    s = SettingsUpdate(**_base(ocr_service_url="http://5.10.248.55:18080"))
    assert s.ocr_service_url == "http://5.10.248.55:18080"


def test_multiple_ocr_urls_normalized_to_comma_join():
    s = SettingsUpdate(**_base(ocr_service_url="http://a:18080 , http://b:18080"))
    assert s.ocr_service_url == "http://a:18080, http://b:18080"


def test_ocr_url_rejects_bad_scheme():
    with pytest.raises(ValidationError):
        SettingsUpdate(**_base(ocr_service_url="http://ok:1, ftp://bad:1"))


def test_ocr_url_rejects_missing_host():
    with pytest.raises(ValidationError):
        SettingsUpdate(**_base(ocr_service_url="http://"))


def test_ocr_url_rejects_empty():
    with pytest.raises(ValidationError):
        SettingsUpdate(**_base(ocr_service_url="   "))


# --- bot_market_data_url: now a comma/space-separated failover pool (HA) -----


def test_bot_market_data_url_empty_is_off():
    # Empty = auto-sell OFF fleet-wide (NOT a validation error).
    s = SettingsUpdate(**_base(bot_market_data_url="   "))
    assert s.bot_market_data_url == ""


def test_single_bot_market_data_url_roundtrips_unchanged():
    s = SettingsUpdate(**_base(bot_market_data_url="http://5.10.248.55:8077"))
    assert s.bot_market_data_url == "http://5.10.248.55:8077"


def test_multiple_bot_market_data_urls_normalized_to_comma_join():
    s = SettingsUpdate(
        **_base(bot_market_data_url="http://5.10.248.55:8077 , http://45.139.10.192:8077")
    )
    assert s.bot_market_data_url == "http://5.10.248.55:8077, http://45.139.10.192:8077"


def test_bot_market_data_url_rejects_bad_scheme():
    with pytest.raises(ValidationError):
        SettingsUpdate(**_base(bot_market_data_url="http://ok:1, ftp://bad:1"))


def test_bot_market_data_url_rejects_missing_host():
    with pytest.raises(ValidationError):
        SettingsUpdate(**_base(bot_market_data_url="http://"))
