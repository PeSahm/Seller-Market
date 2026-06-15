"""Pure config-file renderers for per-agent Docker stacks.

These services produce the four config files that the stacks service
SFTP-pushes into each agent's stack directory on a trading server:

    <server.base_dir>/<agent_id>/
    ├── docker-compose.yml
    ├── .env
    ├── config.ini
    ├── scheduler_config.json
    └── locust_config.json

Design notes
------------
* Renderers are **pure**: no DB access, no SSH, no I/O. They take a
  :class:`StackRenderContext` dataclass and return a ``str``.
* The caller (stacks service) is responsible for decrypting customer
  passwords *before* building the context, so this layer never sees a
  Fernet token and stays trivially testable with golden-file fixtures.
* The dataclasses below are intentionally minimal projections of the
  ORM rows — just enough for the renderers, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence
from uuid import UUID


@dataclass
class CustomerRow:
    """Minimal projection of a ``customers`` row needed to render one section."""

    section_name: str
    username: str
    password_plain: str  # already decrypted by the caller — service stays oblivious
    broker: str
    isin: str
    side: int  # 1 or 2
    # #110 auto-sell: best-buy-queue share count below which the bot sells. None
    # = no auto-sell for this section (the renderer omits the key entirely).
    auto_sell_threshold: Optional[int] = None
    # Auto-sell ONLY: watch an EXISTING holding without buying. The section keeps
    # side=1 (monitor arming is untouched) but the bot skips it in locust + cache
    # warmup so nothing fires at open. MUST stay the LAST field — existing tests
    # construct CustomerRow positionally.
    auto_sell_only: bool = False


@dataclass
class SchedulerJobRow:
    """Minimal projection of a ``scheduler_jobs`` row."""

    name: str  # "cache_warmup" | "run_trading"
    time: str  # "HH:MM:SS"
    enabled: bool
    command: str  # full command string from DB


@dataclass
class LocustConfigRow:
    """Minimal projection of a ``locust_config`` row (per-agent override)."""

    users: int = 10
    spawn_rate: int = 10
    run_time: str = "120s"
    host: str = "https://abc.com"
    processes: int = 1


@dataclass
class StackRenderContext:
    """Everything the renderers need; the stacks service builds this from DB.

    Keeps the renderers pure (no DB access, no SSH) so they're easy to test
    with golden-file fixtures.
    """

    agent_id: UUID
    server_base_dir: str  # e.g. "/root/seller-market/agents"
    agent_image_tag: str  # global setting, e.g. "ghcr.io/pesahm/seller-market-scheduler:latest"
    ocr_service_url: str  # global setting, e.g. "http://5.10.248.55:18080"
    tz: str = "Asia/Tehran"
    customers: Sequence[CustomerRow] = field(default_factory=tuple)
    scheduler_jobs: Sequence[SchedulerJobRow] = field(default_factory=tuple)
    scheduler_enabled: bool = False
    locust: "LocustConfigRow | None" = None
    # Auto-scale the locust user/spawn counts to the number of config sections
    # so locust never spawns fewer users than there are customers (otherwise the
    # excess customers never fire). Off by default for pure golden-file render
    # tests; the stacks service flips it on from the ``enable_locust_autoscale``
    # setting. ``locust_users_multiplier`` is the "3×" knob (``users = 3×sections``).
    autoscale_locust: bool = False
    locust_users_multiplier: int = 3
    # #110 auto-sell: URL of the shared market-data WS service the bot's auto-sell
    # monitor consumes (e.g. "http://5.10.248.55:8077"). EMPTY = auto-sell off →
    # the bot keeps the byte-identical scheduler-only command. Set it to switch the
    # container to bot_entrypoint.py (scheduler + monitor) with MARKET_DATA_URL.
    bot_market_data_url: str = ""
    # Exir order-timing gate: the bot holds every Exir order POST until this
    # Tehran wall-clock instant, then races. Emitted as `fire_at` in Exir
    # config.ini sections only; ephoenix renders byte-identically.
    exir_fire_at: str = "08:44:59.000"


# Re-exports so callers can do
#   `from app.services.rendering import render_compose_yaml, ...`
from app.services.rendering.compose_yaml import render_compose_yaml  # noqa: E402
from app.services.rendering.config_ini import render_config_ini  # noqa: E402
from app.services.rendering.env_file import render_env  # noqa: E402
from app.services.rendering.locust_config import render_locust_config  # noqa: E402
from app.services.rendering.scheduler_config import render_scheduler_config  # noqa: E402

__all__ = [
    "CustomerRow",
    "LocustConfigRow",
    "SchedulerJobRow",
    "StackRenderContext",
    "render_compose_yaml",
    "render_config_ini",
    "render_env",
    "render_locust_config",
    "render_scheduler_config",
]
