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


def render_locust_config(ctx: StackRenderContext) -> str:
    """Render ``locust_config.json`` for an agent stack."""
    cfg = dict(_DEFAULTS)
    if ctx.locust is not None:
        cfg["users"] = ctx.locust.users
        cfg["spawn_rate"] = ctx.locust.spawn_rate
        cfg["run_time"] = ctx.locust.run_time
        cfg["host"] = ctx.locust.host
        cfg["processes"] = ctx.locust.processes
    return json.dumps({"locust": cfg}, indent=2) + "\n"
