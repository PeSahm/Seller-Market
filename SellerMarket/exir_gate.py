"""Exir-only order-fire gate helpers.

Exir brokers penalise orders sent too early. The bot holds every Exir order POST
until a configured wall-clock instant (default 08:44:59.000 Tehran), then runs
the head-of-queue race. ephoenix is unaffected.

The pure decision logic lives here (not in ``locustfile_new``) so it's
unit-testable without importing the locust module — importing ``locustfile_new``
runs ``_create_user_classes()`` at module level, which ``exit(1)``s when there's
no ``config.ini``. ``locustfile_new.on_start`` calls :func:`gate_delay` then
``gevent.sleep`` (cooperative, no CPU spin). Flat top-level module so the bot's
``COPY *.py`` Dockerfile includes it.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as _time
from typing import Optional

logger = logging.getLogger(__name__)

# Default Tehran wall-clock instant to hold Exir POSTs until. Overridable per
# section via the config.ini `fire_at` key (rendered by the mgmt UI), and
# fleet-wide via the `exir_fire_at` setting.
EXIR_DEFAULT_FIRE_AT = "08:44:59.000"


def parse_fire_at(value) -> Optional[_time]:
    """Parse ``"HH:MM:SS"`` or ``"HH:MM:SS.fff"`` into a ``datetime.time``.

    Returns None for blank/invalid input (the gate is then disabled, with a
    warning, for that section — never raises).
    """
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    logger.warning("invalid fire_at %r; Exir gate disabled for this section", value)
    return None


def gate_delay(fire_at_value, now: datetime) -> Optional[float]:
    """Seconds to hold before firing, computed against ``now`` (the scheduler's
    naive local wall-clock — same clock, container ``TZ=Asia/Tehran``).

    * ``None``  → no gate (blank/invalid ``fire_at``) → caller fires normally.
    * ``<= 0``  → already past the fire-time (e.g. a manual mid-day re-run) →
      caller fires IMMEDIATELY. Uses ``now``'s date, so it never rolls to
      tomorrow.
    * ``> 0``   → hold this many seconds (``gevent.sleep``), however large — there
      is NO max-hold clamp (a clamp would wrongly fire an *early* send when the
      run legitimately starts well before the fire-time).
    """
    t = parse_fire_at(fire_at_value or EXIR_DEFAULT_FIRE_AT)
    if t is None:
        return None
    target = datetime.combine(now.date(), t)
    return (target - now).total_seconds()


__all__ = ["EXIR_DEFAULT_FIRE_AT", "parse_fire_at", "gate_delay"]
