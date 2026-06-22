"""Pydantic schemas for scheduler-job CRUD (Phase 5).

The trading bot ships two cron-like jobs per agent stack:

* ``cache_warmup`` — pre-warms the broker session cache before market open.
* ``run_trading`` — kicks off the locust run that places the day's orders.

Both are defined as rows in the ``scheduler_jobs`` table with
``UNIQUE(stack_id, name)``. The bot polls the rendered
``scheduler_config.json`` every second and fires whichever job's ``time``
matches the current wall-clock minute (see
``SellerMarket/scheduler.py:should_run_job``). The bot's dedupe key is
``f"{name}_{date}_{time_str}"`` — meaning a time change on a job that has
already fired today produces a new key and the job re-fires.

Schema layout mirrors :mod:`app.schemas.customer`: a write-side ``Upsert``
model for form submissions, an ``Out`` model for outbound responses, and a
small ``WillReFireToday`` payload the route uses to warn the operator when
their time change would cause an immediate re-fire today.

Command whitelist
-----------------
``ALLOWED_COMMANDS`` is the source of truth for which shell strings can be
written into ``scheduler_config.json``. The bot would refuse anything else
on its side, but defence in depth: the mgmt UI also refuses to *persist*
anything outside the whitelist so a stray DB edit can't trick a future
re-render into shipping ``rm -rf /`` to a customer's server. Operators
should never need to type a custom command — the route layer defaults to
the canonical entry for the given job name when the form omits it.
"""

from __future__ import annotations

import re
from datetime import time as time_type
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The two job names the bot understands. The DB has a matching SAEnum so a
# stray value would be rejected on insert anyway, but surfacing the literal
# at the schema boundary turns a future "I added a third job" mistake into
# a clear 422 instead of an opaque IntegrityError.
JobName = Literal["cache_warmup", "run_trading"]

# Canonical commands the bot will execute. The tuple-of-strings shape lets us
# add alternates later (e.g. a ``--debug`` flavour) without breaking callers
# that just check membership. The first entry is the default the service
# layer falls back to when the route omits ``command``.
#
# Keep this in sync with the rendering layer that materialises
# ``scheduler_config.json``: if the renderer ever needs to format a command
# differently, update both sides at once.
ALLOWED_COMMANDS: dict[str, tuple[str, ...]] = {
    "cache_warmup": ("python cache_warmup.py",),
    "run_trading": ("locust -f locustfile_new.py --headless",),
}

# Default fire times (HH:MM:SS Tehran) seeded onto a freshly-created stack that
# has no schedule, so a new stack is schedulable out of the box (warmup before
# market open, trading run a few minutes before the ~08:45 open). Existing jobs
# are never overwritten — see ``scheduler_jobs.ensure_default_scheduler_jobs``.
DEFAULT_JOB_TIMES: dict[str, str] = {
    "cache_warmup": "08:30:00",
    "run_trading": "08:44:20",
}

# HH:MM:SS, 24-hour. We do NOT use ``datetime.strptime`` here because we want
# the bot's wire format byte-exact (zero-padded, colon-separated) — strptime
# would silently accept ``8:5:3`` and we'd quietly drift away from what the
# bot's scheduler key expects.
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d):([0-5]\d)$")


def is_command_allowed(name: str, command: str) -> bool:
    """Return True iff ``command`` is one of the allowed strings for ``name``.

    Membership check rather than a regex so an attacker can't sneak in
    extra arguments via clever escaping — the only commands that ever reach
    the remote are the exact bytes in :data:`ALLOWED_COMMANDS`.

    Unknown job names return False (rather than raising) so the caller can
    chain this into validation without a try/except. The service layer is
    responsible for separately rejecting unknown job names.
    """
    return command in ALLOWED_COMMANDS.get(name, ())


class SchedulerJobUpsert(BaseModel):
    """Upsert payload for one of the two jobs in a stack.

    ``name`` is supplied as a path / form arg by the route — keeping it out
    of this model means the route can validate it against the URL pattern
    directly and we don't have to deal with the awkward "the form said X
    but the URL said Y" case here.

    ``version`` is REQUIRED for optimistic locking on update; on first-time
    insert the route passes ``version=0`` and the service treats that as
    "create, please". The service layer also gracefully handles the
    UNIQUE-constraint race where two callers both think it's a create.

    ``command`` is optional: when ``None`` the service falls back to
    ``ALLOWED_COMMANDS[name][0]`` (the canonical default). When set, the
    service rejects anything outside :data:`ALLOWED_COMMANDS`.
    """

    time: str = Field(min_length=8, max_length=8)
    enabled: bool
    command: Optional[str] = None
    version: int = Field(..., ge=0)

    @field_validator("time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        """HH:MM:SS, 24-hour, zero-padded.

        We reject HH:MM (no seconds) because the bot's scheduler key
        includes the seconds portion verbatim — accepting ``08:15`` here
        and silently appending ``:00`` would mean two admins who think
        they're entering the same time end up with two different dedupe
        keys ``...08:15:00`` and ``...08:15``.
        """
        if not _TIME_RE.fullmatch(v):
            raise ValueError("time must be HH:MM:SS (24h)")
        return v


class SchedulerJobOut(BaseModel):
    """Outbound representation of a ``scheduler_jobs`` row.

    The DB-side ``time`` column is a ``sa.Time`` (Python ``datetime.time``),
    which serialises to ``"HH:MM:SS"`` via pydantic's built-in JSON encoder.
    The UI templates expect that shape verbatim — the route renders the
    Out model with ``.model_dump(mode='json')`` and the template iterates.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    stack_id: UUID
    name: str
    time: time_type
    enabled: bool
    command: str
    version: int


class WillReFireToday(BaseModel):
    """Best-effort heuristic payload for the "this will re-fire today" banner.

    The bot's scheduler keys executions on ``(name, date, time_str)``. If we
    change the time on a row that has already fired today AND the new time
    is still in the future today, the bot will produce a fresh dedupe key
    and re-fire. The UI shows a warning before the operator clicks save so
    they don't accidentally double-run a trading session.

    ``will_refire`` is intentionally pessimistic: when in doubt we say
    True, on the theory that a false-positive warning ("this might re-fire")
    is much less damaging than a false-negative ("everything's fine") that
    leads to a double-run.
    """

    will_refire: bool
    reason: str = ""


__all__ = [
    "ALLOWED_COMMANDS",
    "JobName",
    "SchedulerJobOut",
    "SchedulerJobUpsert",
    "WillReFireToday",
    "is_command_allowed",
]
