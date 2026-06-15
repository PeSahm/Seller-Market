"""Pydantic schema for the admin Settings form.

Form fields map 1:1 onto rows in the ``settings`` table. Validation here is
defensive: the UI already restricts shape via ``maxlength``/``required`` HTML
attributes, but a malicious or scripted POST bypasses those, so we re-check
every field server-side.
"""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

# Characters that would let an admin smuggle arbitrary shell into the image
# tag (it ends up interpolated into a docker-compose.yml literal). This isn't
# a full Docker reference parser — we just reject the obvious foot-guns.
_FORBIDDEN_TAG_CHARS = frozenset(" \t;&|$`<>\"'\\")


class SettingsUpdate(BaseModel):
    """Validated payload for ``POST /admin/settings``."""

    ocr_service_url: str = Field(min_length=1, max_length=512)
    agent_image_tag: str = Field(min_length=1, max_length=255)
    # Per-stack ``processes`` ceiling for locust load runs (Phase 5). The
    # static ``le=32`` here mirrors the hard ceiling on
    # :class:`app.schemas.locust.LocustUpsert.processes` — a value larger
    # than the typical host core count is never sensible. The *operational*
    # cap (default 4) is whatever the admin sets; the service layer
    # rejects per-stack values exceeding it.
    agent_locust_processes_cap: int = Field(default=4, ge=1, le=32)
    # Auto-sell (#110): URL the BOT stacks use to reach the shared market-data WS
    # service (e.g. "http://5.10.248.55:8077"). EMPTY = auto-sell OFF fleet-wide
    # (stacks keep the scheduler-only command). Setting it flips each stack to
    # bot_entrypoint.py + MARKET_DATA_URL on the next Redeploy.
    bot_market_data_url: str = Field(default="", max_length=512)
    # Exir order-timing gate: the bot holds Exir order POSTs until this Tehran
    # wall-clock instant, then races (ephoenix unaffected). "HH:MM:SS[.fff]".
    exir_fire_at: str = Field(default="08:44:59.000", max_length=16)

    @field_validator("ocr_service_url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        v = v.strip()
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("ocr_service_url must be http:// or https://")
        if not parsed.netloc:
            raise ValueError("ocr_service_url is missing host")
        return v

    @field_validator("bot_market_data_url")
    @classmethod
    def _check_bot_market_data_url(cls, v: str) -> str:
        # Empty is valid and means "auto-sell off fleet-wide".
        v = v.strip()
        if not v:
            return ""
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("bot_market_data_url must be http:// or https:// (or empty)")
        if not parsed.netloc:
            raise ValueError("bot_market_data_url is missing host")
        return v

    @field_validator("exir_fire_at")
    @classmethod
    def _check_exir_fire_at(cls, v: str) -> str:
        from datetime import datetime as _dt

        v = (v or "").strip()
        if not v:
            raise ValueError("exir_fire_at is required (HH:MM:SS or HH:MM:SS.fff)")
        for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
            try:
                _dt.strptime(v, fmt)
                return v
            except ValueError:
                continue
        raise ValueError("exir_fire_at must be HH:MM:SS or HH:MM:SS.fff (24h)")

    @field_validator("agent_image_tag")
    @classmethod
    def _check_image_tag(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("agent_image_tag is required")
        if any(c.isspace() or c in _FORBIDDEN_TAG_CHARS for c in v):
            raise ValueError("agent_image_tag contains invalid characters")
        return v
