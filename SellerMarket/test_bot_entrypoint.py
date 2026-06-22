"""Hermetic tests for bot_entrypoint config-precedence helpers."""
from __future__ import annotations

import bot_entrypoint
import runtime_config


def test_market_data_url_runtime_first(monkeypatch):
    runtime_config.reset_cache()
    monkeypatch.setenv("MARKET_DATA_URL", "http://env:8077")
    monkeypatch.setattr(runtime_config, "_snapshot", lambda: {})
    assert bot_entrypoint._market_data_url() == "http://env:8077"   # env when no runtime
    monkeypatch.setattr(runtime_config, "_snapshot",
                        lambda: {"market_data_url": "http://rt:8077"})
    assert bot_entrypoint._market_data_url() == "http://rt:8077"    # runtime wins


def test_market_data_url_empty_default(monkeypatch):
    monkeypatch.delenv("MARKET_DATA_URL", raising=False)
    monkeypatch.setattr(runtime_config, "_snapshot", lambda: {})
    assert bot_entrypoint._market_data_url() == ""
