"""Renderer for the per-agent ``locust_config.json``.

If the context has no :class:`LocustConfigRow` we emit the fleet defaults;
otherwise we apply the per-agent override values one-for-one.
"""

from __future__ import annotations

import json

from app.services.rendering import StackRenderContext

_DEFAULTS: dict[str, object] = {
    "users": 10,
    "spawn_rate": 10,
    "run_time": "120s",
    "host": "https://abc.com",
    "processes": 1,
}

# Schema caps (mirror app/schemas/locust.py: users<=10000, spawn_rate<=1000) so
# auto-scaling a very large agent can never render an out-of-range value.
_USERS_CAP = 10000
_SPAWN_CAP = 1000


def compute_locust_targets(
    num_sections: int,
    *,
    multiplier: int = 3,
    floor_users: int = 10,
) -> tuple[int, int]:
    """Auto-scale ``(users, spawn_rate)`` to the number of config sections.

    ``locustfile_new`` creates **one user-class per config section** but locust
    only spawns ``users`` instances across them — so with fewer users than
    sections, the excess customers are prepared but never fire. We set
    ``users = max(multiplier × sections, floor_users)`` (default 3× for
    head-of-queue depth; ``floor_users`` lets a manually-set value act as a
    floor) and ``spawn_rate = sections`` so every user spawns fast at the open.
    Both are clamped to the schema caps.
    """
    sections = max(0, int(num_sections))
    users = max(multiplier * sections, int(floor_users), 1)
    users = min(users, _USERS_CAP)
    spawn = min(max(sections, 1), _SPAWN_CAP)
    return users, spawn


def render_locust_config(ctx: StackRenderContext) -> str:
    """Render ``locust_config.json`` for an agent stack.

    When ``ctx.autoscale_locust`` is set, ``users``/``spawn_rate`` are derived
    from the customer-section count (see :func:`compute_locust_targets`); the
    per-agent override row's ``users`` acts as a floor. Otherwise the override
    (or fleet default) values are emitted verbatim — the pre-feature behaviour.
    """
    cfg = dict(_DEFAULTS)
    if ctx.locust is not None:
        cfg["users"] = ctx.locust.users
        cfg["spawn_rate"] = ctx.locust.spawn_rate
        cfg["run_time"] = ctx.locust.run_time
        cfg["host"] = ctx.locust.host
        cfg["processes"] = ctx.locust.processes
    if ctx.autoscale_locust:
        users, spawn = compute_locust_targets(
            len(ctx.customers),
            multiplier=ctx.locust_users_multiplier or 3,
            floor_users=int(cfg["users"]),
        )
        cfg["users"] = users
        cfg["spawn_rate"] = spawn
    return json.dumps({"locust": cfg}, indent=2) + "\n"
