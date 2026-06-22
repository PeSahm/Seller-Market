"""Unit tests for the admin Settings form schema (``app.schemas.settings_page``).

Focus: the ``ocr_service_url`` validator now accepts a comma/space-separated
LIST of endpoints (client-side OCR pool — HA plan WS1) while keeping a single
URL byte-identical.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.settings_page import SettingsUpdate, parse_advanced_runtime


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


# --- bot_rt_* runtime / endpoint fields (disaster set) -----------------------


def test_bot_rt_defaults_match_hardcoded():
    s = SettingsUpdate(**_base())
    assert s.bot_rt_ephoenix_domain == "ephoenix.ir"
    assert s.bot_rt_ephoenix_md_host == "marketdatagw"
    assert s.bot_rt_ib_domain == "ibtrader.ir"
    assert s.bot_rt_ib_portfolio_shard == "api8"
    assert s.bot_rt_exir_domain == "exirbroker.com"
    assert s.bot_rt_auto_sell_window == "09:00-12:30"


def test_bot_rt_md_host_override():
    s = SettingsUpdate(**_base(bot_rt_ephoenix_md_host="newmdgw"))
    assert s.bot_rt_ephoenix_md_host == "newmdgw"


def test_bot_rt_host_rejects_percent_and_space():
    # % would break the bot's interpolating ConfigParser; whitespace is invalid.
    with pytest.raises(ValidationError):
        SettingsUpdate(**_base(bot_rt_ephoenix_domain="ephoenix.ir%"))
    with pytest.raises(ValidationError):
        SettingsUpdate(**_base(bot_rt_ephoenix_domain="ephoenix .ir"))
    with pytest.raises(ValidationError):
        SettingsUpdate(**_base(bot_rt_ephoenix_md_host="  "))


def test_bot_rt_window_format():
    assert SettingsUpdate(**_base(bot_rt_auto_sell_window="13:00-14:30")).bot_rt_auto_sell_window == "13:00-14:30"
    with pytest.raises(ValidationError):
        SettingsUpdate(**_base(bot_rt_auto_sell_window="notawindow"))


def test_bot_rt_fee_bounds():
    assert SettingsUpdate(**_base(bot_rt_exir_fallback_buy_fee=0.01)).bot_rt_exir_fallback_buy_fee == 0.01
    with pytest.raises(ValidationError):
        SettingsUpdate(**_base(bot_rt_exir_fallback_buy_fee=0))     # gt=0
    with pytest.raises(ValidationError):
        SettingsUpdate(**_base(bot_rt_exir_fallback_buy_fee=0.5))   # lt=0.1


# --- Advanced raw editor (escape hatch) --------------------------------------


def test_parse_advanced_runtime_ok():
    out = parse_advanced_runtime(
        "bot_rt_endpoint_ib_order = https://x/y\n"
        "# a comment\n"
        "\n"
        "bot_rt_rlc_base_url=https://core//H"
    )
    assert out == {
        "bot_rt_endpoint_ib_order": "https://x/y",
        "bot_rt_rlc_base_url": "https://core//H",
    }


def test_parse_advanced_runtime_empty():
    assert parse_advanced_runtime("") == {}
    assert parse_advanced_runtime("   \n# only a comment\n") == {}


def test_parse_advanced_runtime_rejects_non_bot_rt_key():
    with pytest.raises(ValueError):
        parse_advanced_runtime("evil_key = x")


def test_parse_advanced_runtime_rejects_malformed_key():
    with pytest.raises(ValueError):
        parse_advanced_runtime("bot_rt_BAD UPPER = x")
    with pytest.raises(ValueError):
        parse_advanced_runtime("no_equals_sign")
