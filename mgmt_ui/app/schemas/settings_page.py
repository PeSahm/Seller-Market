"""Pydantic schema for the admin Settings form.

Form fields map 1:1 onto rows in the ``settings`` table. Validation here is
defensive: the UI already restricts shape via ``maxlength``/``required`` HTML
attributes, but a malicious or scripted POST bypasses those, so we re-check
every field server-side.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

# Characters that would let an admin smuggle arbitrary shell into the image
# tag (it ends up interpolated into a docker-compose.yml literal). This isn't
# a full Docker reference parser — we just reject the obvious foot-guns.
_FORBIDDEN_TAG_CHARS = frozenset(" \t;&|$`<>\"'\\")

# Host/domain tokens for the bot ``[runtime]`` overrides. ``%`` is rejected
# because the value is rendered into config.ini, which the bot's auto-sell
# monitor reads with an INTERPOLATING ConfigParser (a lone ``%`` would raise).
_FORBIDDEN_HOSTISH_CHARS = frozenset(" \t;&|$`<>\"'\\%/")
_WINDOW_RE = re.compile(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")
# Mofid fire-window times: HH:MM:SS with optional milliseconds (".450").
_HMS_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}(\.\d{1,3})?$")
# Advanced editor keys: a strict allowlist (prefix-scoped) so the raw textarea
# can never write an arbitrary settings row.
_BOT_RT_KEY_RE = re.compile(r"^bot_rt_[a-z0-9_]+$")


def parse_advanced_runtime(text: str) -> dict[str, str]:
    """Parse the Advanced [runtime] editor (``bot_rt_<key> = <value>`` lines).

    Blank lines and ``#`` comments are ignored. Returns ``{setting_key: value}``.
    Raises ``ValueError`` on a malformed line or a key outside the ``bot_rt_``
    allowlist (so the raw editor can never write an unrelated settings row).
    """
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid line (expected 'key = value'): {line!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        if not _BOT_RT_KEY_RE.match(key):
            raise ValueError(
                f"invalid runtime key {key!r} (must match bot_rt_<name>, "
                "lowercase letters/digits/underscores)"
            )
        out[key] = value.strip()
    return out


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
    # Auto-sell (#110): URL(s) the BOT stacks use to reach the market-data WS
    # service (e.g. "http://5.10.248.55:8077"). EMPTY = auto-sell OFF fleet-wide
    # (stacks keep the scheduler-only command). Setting it flips each stack to
    # bot_entrypoint.py + MARKET_DATA_URL on the next Redeploy. May be a
    # comma/space-separated FAILOVER pool (the bot tries them in order, preferring
    # the first) so a sidecar outage needs no redeploy.
    bot_market_data_url: str = Field(default="", max_length=512)

    # --- Bot runtime / endpoints (DB-pushed [runtime], disaster set) -------
    # Every default equals the bot's current hardcoded literal, so behaviour is
    # unchanged until edited. Stored as bot_rt_<suffix> rows; rendered into the
    # bot's config.ini [runtime] section and pushed fleet-wide instantly.
    bot_rt_ephoenix_domain: str = Field(default="ephoenix.ir", max_length=255)
    bot_rt_ephoenix_md_host: str = Field(default="marketdatagw", max_length=255)
    bot_rt_ib_domain: str = Field(default="ibtrader.ir", max_length=255)
    bot_rt_ib_md_host: str = Field(default="mdapi", max_length=255)
    bot_rt_ib_portfolio_shard: str = Field(default="api8", max_length=255)
    bot_rt_exir_domain: str = Field(default="exirbroker.com", max_length=255)
    bot_rt_exir_fallback_buy_fee: float = Field(default=0.005, gt=0, lt=0.1)
    bot_rt_auto_sell_window: str = Field(default="09:00-12:30", max_length=32)
    bot_rt_auto_sell_confirm_secs: float = Field(default=5.0, ge=0)
    # Mofid / Orbis firing: how many drafts to create + the batch-send window.
    bot_rt_mofid_draft_count: int = Field(default=1, ge=1, le=50)
    bot_rt_mofid_run_time: str = Field(default="08:44:00", max_length=16)
    bot_rt_mofid_window_start: str = Field(default="08:44:58.450", max_length=16)
    bot_rt_mofid_window_end: str = Field(default="08:45:00.900", max_length=16)

    @field_validator("ocr_service_url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        # One OR MORE endpoints, comma/space-separated (client-side OCR pool —
        # bots/mgmt try them in order with failover). A single URL round-trips
        # unchanged. Each token must be a valid http(s) URL with a host.
        tokens = [t.strip() for t in (v or "").replace(",", " ").split()]
        tokens = [t for t in tokens if t]
        if not tokens:
            raise ValueError("ocr_service_url requires at least one http(s) URL")
        for t in tokens:
            parsed = urlparse(t)
            if parsed.scheme not in ("http", "https"):
                raise ValueError("ocr_service_url must be http:// or https://")
            if not parsed.netloc:
                raise ValueError("ocr_service_url is missing host")
        return ", ".join(tokens)

    @field_validator("bot_market_data_url")
    @classmethod
    def _check_bot_market_data_url(cls, v: str) -> str:
        # Empty is valid and means "auto-sell off fleet-wide". Otherwise one OR
        # MORE endpoints, comma/space-separated (a failover pool — the bot tries
        # them in order, preferring the first, so a sidecar outage needs no
        # redeploy). A single URL round-trips unchanged; the list normalises to a
        # comma-space-joined form (rendered verbatim into MARKET_DATA_URL).
        tokens = [t.strip() for t in (v or "").replace(",", " ").split()]
        tokens = [t for t in tokens if t]
        if not tokens:
            return ""
        for t in tokens:
            parsed = urlparse(t)
            if parsed.scheme not in ("http", "https"):
                raise ValueError("bot_market_data_url must be http:// or https:// (or empty)")
            if not parsed.netloc:
                raise ValueError("bot_market_data_url is missing host")
        return ", ".join(tokens)

    @field_validator("agent_image_tag")
    @classmethod
    def _check_image_tag(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("agent_image_tag is required")
        if any(c.isspace() or c in _FORBIDDEN_TAG_CHARS for c in v):
            raise ValueError("agent_image_tag contains invalid characters")
        return v

    @field_validator(
        "bot_rt_ephoenix_domain", "bot_rt_ephoenix_md_host", "bot_rt_ib_domain",
        "bot_rt_ib_md_host", "bot_rt_ib_portfolio_shard", "bot_rt_exir_domain",
    )
    @classmethod
    def _check_hostish(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("must not be empty")
        if any(c.isspace() or c in _FORBIDDEN_HOSTISH_CHARS for c in v):
            raise ValueError("contains invalid characters")
        return v

    @field_validator("bot_rt_auto_sell_window")
    @classmethod
    def _check_window(cls, v: str) -> str:
        v = (v or "").strip()
        if not _WINDOW_RE.match(v):
            raise ValueError("window must look like HH:MM-HH:MM")
        return v

    @field_validator(
        "bot_rt_mofid_run_time", "bot_rt_mofid_window_start", "bot_rt_mofid_window_end"
    )
    @classmethod
    def _check_hms(cls, v: str) -> str:
        v = (v or "").strip()
        if not _HMS_RE.match(v):
            raise ValueError("fire time must look like HH:MM:SS or HH:MM:SS.mmm")
        return v
