"""Pydantic schemas for the customer-distribution service (Phase 4).

The distribution service is what turns an agent-declared :class:`Customer`
row into a concrete ``(server, agent_stack)`` placement. The admin UI's
"assign / move / unassign" buttons feed into the corresponding service
functions through these schemas; the auto-policy resolver is parameterised
by :class:`PolicySet`.

Why a separate schemas module?
------------------------------
:mod:`app.schemas.customer` already covers the CRUD shape of a customer
row. Mixing assignment-side request/response models in there would couple
the agent-facing CRUD page to the admin-only distribution page and would
also bloat the customer schemas with fields no agent form ever submits.
Keeping the two side by side (one per concern) lets each router import
exactly the contract it needs and keeps the OpenAPI surface tidy.

Secret hygiene
--------------
None of these models touch credential material. ``AssignmentResult`` carries
only identifiers and a human-readable message; ``PolicySet`` carries the
operator's choice of distribution algorithm plus an optional default server
id. Nothing here goes near ``password_enc`` or ``ssh_secret_ref``, so there
is no per-field redaction to do.
"""

from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# The four distribution algorithms the operator can pick from. Kept in sync
# with the DB-side ``distribution_policy`` enum declared in
# :mod:`app.models.customers`. If a new policy is added there, this Literal
# must be widened too — pydantic validates on the way in so an out-of-range
# string never reaches the resolver.
Policy = Literal["manual", "round_robin", "least_customers", "broker_affinity"]


class AssignmentRequest(BaseModel):
    """Admin-submitted: "put this customer on this server, now".

    The router maps the URL path's ``customer_id`` to the service call and
    pulls ``server_id`` out of this body. We deliberately do NOT accept a
    ``stack_id`` here — the stack is derived from ``(server, agent)`` by
    :func:`app.services.stacks.find_or_create_stack`, so allowing the
    caller to override it would risk a mismatch with the agent column on the
    customer row.
    """

    server_id: UUID


class MoveRequest(BaseModel):
    """Admin-submitted: "move this customer to a different server".

    Named ``new_server_id`` rather than ``server_id`` so the form is
    self-documenting on screen ("move to: __") and so the route can also
    surface the OLD server in the same template without name collision.
    """

    new_server_id: UUID


class AssignmentResult(BaseModel):
    """Returned by assign / unassign / move so the router can render a flash.

    The template's flash banner shows ``message`` verbatim, then the table
    refresh uses the ``new_*`` / ``old_*`` ids to highlight the affected
    rows. ``affected_stack_ids`` is the union of "old stack" + "new stack"
    for a move (so the UI knows which two config.ini SFTP-pushes ran) and
    just the one for assign / unassign.

    The model is intentionally low-ceremony: no nested response wrappers,
    no envelope. The router renders directly off these fields.
    """

    ok: bool
    customer_id: UUID
    old_server_id: Optional[UUID] = None
    new_server_id: Optional[UUID] = None
    old_stack_id: Optional[UUID] = None
    new_stack_id: Optional[UUID] = None
    # ``Field(default_factory=list)`` so each instance gets its own list —
    # a bare ``[]`` as default would be SHARED across instances (classic
    # Python footgun). Pydantic v2 normally guards against this but being
    # explicit keeps the intent obvious to a reviewer.
    affected_stack_ids: list[UUID] = Field(default_factory=list)
    message: str = ""


class PolicySet(BaseModel):
    """Operator's choice of distribution policy.

    Used for both the global default and the per-agent override on the
    same admin form. The router decides which service function to call
    (set_global_policy vs. set_agent_policy) based on which page submitted
    the form; ``scope`` and ``agent_id`` echo the URL for an extra sanity
    check on the way in.

    ``from_attributes=True`` so the service can return an ORM row and the
    router can ``PolicySet.model_validate(row)`` without manual unpacking.
    """

    model_config = ConfigDict(from_attributes=True)

    scope: Literal["global", "agent"] = "global"
    agent_id: Optional[UUID] = None
    policy: Policy = "manual"
    default_server_id: Optional[UUID] = None


__all__ = [
    "AssignmentRequest",
    "AssignmentResult",
    "MoveRequest",
    "Policy",
    "PolicySet",
]
