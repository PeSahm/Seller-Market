"""Renderer for the per-agent ``scheduler_config.json``.

The upstream ``scheduler.py`` re-reads this file every ~1 second of its
poll loop, so no reload hook / SIGHUP is needed — once the stacks service
writes a new file atomically, the change is picked up within a second.
"""

from __future__ import annotations

import json

from app.services.rendering import StackRenderContext


def render_scheduler_config(ctx: StackRenderContext) -> str:
    """Render ``scheduler_config.json`` from the context's job rows."""
    payload = {
        "enabled": ctx.scheduler_enabled,
        "jobs": [
            {
                "name": j.name,
                "time": j.time,
                "command": j.command,
                "enabled": j.enabled,
            }
            for j in ctx.scheduler_jobs
        ],
    }
    return json.dumps(payload, indent=2) + "\n"
