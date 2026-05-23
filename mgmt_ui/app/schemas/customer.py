"""Pydantic schemas for customer CRUD.

Post-migration 0003, ``Customer`` is account-shaped: (agent, broker,
username, password, display_name). The per-instrument fields (isin,
side, comment) moved to :mod:`app.schemas.trade_instruction`.

Secret hygiene
--------------
:attr:`CustomerCreate.password` and :attr:`CustomerUpdate.password` are
write-only — they are accepted on the form but MUST never appear on an
outbound :class:`CustomerOut`. The model column ``password_enc`` (Fernet
ciphertext) is also deliberately excluded from :class:`CustomerOut` so it
can't leak via a JSON response, audit-log payload, or a debug template
dump.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# The Iranian brokers supported by SellerMarket today. Kept in sync with
# :class:`SellerMarket.broker_enum.BrokerCode` — if a broker is added there
# this tuple (and the ``Broker`` ``Literal``) must be widened too.
BROKERS = (
    "gs",
    "bbi",
    "shahr",
    "ib",
    "karamad",
    "tejarat",
    "ebb",
    "hbc",
    "rabin",
    "ayandeh",
    "farabi",
)

# Pydantic v2 uses the ``Literal`` directly for both validation and JSON-schema
# generation. We duplicate the values here rather than build it from
# ``BROKERS`` because ``Literal[*tuple]`` unpacking is a 3.11+ syntax that some
# tooling (mypy, ruff) still chokes on intermittently.
Broker = Literal[
    "gs",
    "bbi",
    "shahr",
    "ib",
    "karamad",
    "tejarat",
    "ebb",
    "hbc",
    "rabin",
    "ayandeh",
    "farabi",
]


class CustomerCreate(BaseModel):
    """What agents (and admins) submit on the create-customer form.

    No per-instrument fields here — those go to TradeInstruction via the
    customer detail page.
    """

    display_name: str = Field(min_length=1, max_length=255)
    broker: Broker
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=512)


class CustomerUpdate(BaseModel):
    """Partial update with optimistic locking.

    ``version`` is REQUIRED for every update — the caller must echo the
    version they read from the row. A mismatch raises
    :class:`app.services.customers.OptimisticLockError` which the router
    translates to HTTP 409. This prevents two admins (or an admin and an
    agent) racing on the same row from silently overwriting each other.

    Every other field is optional; only the explicitly-set ones are touched
    on the underlying row. ``password=None`` means "keep the current
    password", not "set the password to empty" — to clear a password the
    caller would have to send a non-empty placeholder, which is intentional
    (we never want a blank credential reaching the SSH-shipped ``config.ini``).
    """

    display_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    broker: Optional[Broker] = None
    username: Optional[str] = Field(default=None, min_length=1, max_length=255)
    # ``password`` is only set when the caller chose to rotate it. The service
    # layer Fernet-encrypts the value into ``password_enc``.
    password: Optional[str] = Field(default=None, min_length=1, max_length=512)
    version: int = Field(..., ge=1)


class CustomerOut(BaseModel):
    """Outbound representation of a Customer row.

    Deliberately omits ``password_enc`` (the Fernet ciphertext) so we cannot
    accidentally leak it via JSON responses, audit-log payloads, or template
    dumps. The render layer reads the ciphertext directly off the ORM row,
    never through this schema.

    ``trade_count`` is a render hint populated by the list endpoint —
    not a stored column.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    server_id: Optional[UUID]
    stack_id: Optional[UUID]
    assignment_status: str
    display_name: str
    username: str
    broker: str
    version: int
    created_at: datetime
    updated_at: datetime
    trade_count: Optional[int] = None
