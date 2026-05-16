"""Pydantic schemas for the per-stack locust load-test config (Phase 5).

Each :class:`app.models.scheduler.LocustConfig` row holds the inputs the bot
hands to ``locust`` (users, spawn rate, run time, target host, processes) for
a single agent stack. There's at most ONE row per stack — the upsert pattern
keeps the editing UX simple (a single form, no list view, no per-row delete).

Risk-register guardrails
------------------------
Two of the fields encode hard environmental constraints that Phase 2 surfaced
during the bot review; documenting them here rather than in the router keeps
them tested in isolation and visible to anyone hand-editing a payload:

* ``run_time < 600s``: the legacy bot's scheduler invokes
  ``subprocess.run(..., timeout=600)`` (SellerMarket/scheduler.py around line
  227). Any locust invocation that asks for ≥ 600s is silently killed
  mid-run by the watchdog — the agent ends up with truncated metrics and no
  obvious error message. We refuse the value at the schema boundary so the
  admin sees a clear 422 instead of debugging a half-finished run.
* ``processes`` upper cap: a 32-core fleet host can be monopolised by a
  single agent if they crank processes too high. The hard ceiling here is
  ``32`` (process count can never sensibly exceed the host core count), but
  the *operational* cap is dynamic — admins tune it via the
  ``agent_locust_processes_cap`` setting (default 4). That dynamic cap lives
  in the service layer, NOT here, because pydantic field validators have no
  DB access.

Host validation mirrors :class:`app.schemas.settings_page.SettingsUpdate` —
``http`` / ``https`` only, must have a netloc.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


# run_time format: ``<int><s|m|h>``. Locust itself accepts richer strings
# (``1h30m`` etc.) but we keep the surface area tight — admins overwhelmingly
# write ``120s`` or ``5m``, and a stricter shape means the validator can
# return a single, byte-exact error message instead of a regex soup.
_RUN_TIME_RE = re.compile(r"^(\d+)([smh])$")


def parse_run_time_seconds(value: str) -> int:
    """Convert a ``run_time`` string into total seconds.

    Accepts ``<int>s``, ``<int>m``, ``<int>h``. Anything else — including
    bare integers, days (``"1d"``), compound forms (``"1h30m"``), or
    floating-point values — raises ``ValueError``. We intentionally do NOT
    silently coerce ``"120"`` to ``"120s"``: a caller who forgot the unit
    might mean minutes, and guessing wrong silently drops them into the
    600-second guillotine.

    The function is exported so callers (the service layer, tests) can run
    the parse without going through full pydantic validation.
    """
    m = _RUN_TIME_RE.fullmatch(value)
    if not m:
        raise ValueError("run_time must be <int><s|m|h>, e.g. 120s, 5m, 1h")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    # "h"
    return n * 3600


# Hard ceiling: the legacy bot's scheduler kills any locust subprocess after
# 600 s. We refuse anything ``>= 600`` (NOT ``> 600``) so a value of exactly
# 600s is also refused — the watchdog wins races at the boundary and the run
# would be killed before locust prints its summary.
RUN_TIME_HARD_CEILING_SECONDS = 600


class LocustUpsert(BaseModel):
    """Upsert payload for a stack's single locust config row.

    The pattern is "create-or-update": the router doesn't carry a separate
    create / update schema because there's at most one row per stack. The
    caller MUST echo ``version`` from the row they read; the service layer
    rejects mismatches with :class:`app.services.locust_configs.OptimisticLockError`.
    On a fresh insert the caller sends ``version=0`` — see the service docstring.

    The static caps below (``users <= 10000``, ``spawn_rate <= 1000``,
    ``processes <= 32``) are belt-and-braces sanity checks: a typo that
    asks for a million users would otherwise crash the bot host. The
    *operational* cap on ``processes`` is the dynamic admin setting; this
    static 32 is just "no value larger than this is ever sensible".
    """

    users: int = Field(..., ge=1, le=10000)
    spawn_rate: int = Field(..., ge=1, le=1000)
    run_time: str = Field(..., min_length=2, max_length=16)
    host: str = Field(..., min_length=1, max_length=512)
    processes: int = Field(..., ge=1, le=32)
    # Fresh inserts pass ``version=0``; updates echo the row's current
    # ``version``. The service layer handles both paths.
    version: int = Field(..., ge=0)

    @field_validator("run_time")
    @classmethod
    def _check_run_time(cls, v: str) -> str:
        secs = parse_run_time_seconds(v)
        if secs >= RUN_TIME_HARD_CEILING_SECONDS:
            raise ValueError(
                f"run_time must be < 600s (bot's subprocess timeout); got {secs}s"
            )
        if secs < 1:
            raise ValueError("run_time must be at least 1s")
        return v

    @field_validator("host")
    @classmethod
    def _check_host(cls, v: str) -> str:
        v = v.strip()
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("host must be http:// or https://")
        if not parsed.netloc:
            raise ValueError("host is missing the network location")
        return v


class LocustOut(BaseModel):
    """Outbound representation of a :class:`LocustConfig` row.

    Plain pass-through of the model columns — no secret material lives on
    this table so there's no hygiene filter like the one on
    :class:`app.schemas.customer.CustomerOut`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    stack_id: UUID
    users: int
    spawn_rate: int
    run_time: str
    host: str
    processes: int
    version: int


__all__ = [
    "LocustOut",
    "LocustUpsert",
    "RUN_TIME_HARD_CEILING_SECONDS",
    "parse_run_time_seconds",
]
