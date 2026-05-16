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

    @field_validator("agent_image_tag")
    @classmethod
    def _check_image_tag(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("agent_image_tag is required")
        if any(c in _FORBIDDEN_TAG_CHARS for c in v):
            raise ValueError("agent_image_tag contains invalid characters")
        return v
