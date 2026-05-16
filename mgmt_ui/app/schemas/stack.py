"""Pydantic schemas for agent-stack provisioning (Phase 3).

These models gate the data shape on the HTTP boundary for the stack
provisioner. The service layer (:mod:`app.services.stacks`) consumes them
and the router never sees a raw ORM row.

Secret hygiene
--------------
:class:`StackOut` is a pure projection of :class:`~app.models.stacks.AgentStack`
and contains no secret material — by construction, ``agent_stacks`` rows
themselves carry only identifiers and operational state. The
:class:`StackActionResult` includes a ``log_tail`` for the admin to confirm
that a compose command actually ran; the service layer is responsible for
redacting any obvious secret material from that string before constructing
the result (today: docker compose output rarely echoes secrets, but the
service still passes the value through a redact helper as defence-in-depth).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class StackOut(BaseModel):
    """Outbound representation of an :class:`~app.models.stacks.AgentStack` row.

    The ORM row only carries identifiers (server / agent ids), a stack
    directory path, the compose project name, and operational state, so this
    schema mirrors it one-to-one. There is no equivalent of ``ssh_secret_ref``
    to scrub here.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    server_id: UUID
    agent_id: UUID
    stack_dir: str
    compose_project: str
    status: str
    deployed_at: Optional[datetime]


class StackActionResult(BaseModel):
    """Result of a provision / redeploy / deprovision action.

    The router renders this back to the admin so they can confirm the action
    completed. ``log_tail`` is the last 20 lines of stdout/stderr from the
    underlying ``docker compose`` command — enough to spot a failure mode
    (e.g. image pull denied, port already bound) without flooding the page.
    The service layer redacts obvious secret material from ``log_tail``
    before it lands here; this is belt-and-braces because compose output
    rarely echoes anything sensitive, but ``.env`` lines may be reprinted on
    syntax error.
    """

    ok: bool
    stack_id: UUID
    status: str  # final status after the action ("up" | "down" | "deprovisioning")
    message: str
    log_tail: str = ""
