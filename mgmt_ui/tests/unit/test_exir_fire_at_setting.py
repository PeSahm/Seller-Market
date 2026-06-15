"""The `exir_fire_at` setting default + its SettingsUpdate validator."""
from __future__ import annotations

import pytest

from app.schemas.settings_page import SettingsUpdate
from app.services import settings_store

_BASE = {
    "ocr_service_url": "http://1.2.3.4:18080",
    "agent_image_tag": "ghcr.io/pesahm/seller-market:latest",
    "agent_locust_processes_cap": 4,
}


def test_default_is_0844_59() -> None:
    assert settings_store.DEFAULTS["exir_fire_at"] == "08:44:59.000"


@pytest.mark.parametrize("val", ["08:44:59.000", "08:44:59", "08:45:00.500", "00:00:00", "23:59:59"])
def test_validator_accepts_valid(val: str) -> None:
    assert SettingsUpdate(**_BASE, exir_fire_at=val).exir_fire_at == val


@pytest.mark.parametrize("val", ["25:00:00", "8:44", "", "nonsense", "08:60:00", "08:44:99"])
def test_validator_rejects_invalid(val: str) -> None:
    with pytest.raises(Exception):  # pydantic ValidationError wraps the ValueError
        SettingsUpdate(**_BASE, exir_fire_at=val)


def test_validator_default_when_omitted() -> None:
    # Field default applies when the form omits it (older clients).
    assert SettingsUpdate(**_BASE).exir_fire_at == "08:44:59.000"
