"""Guards for the orphaned-active-customer no-trade bug.

A deprovisioned stack used to leave its customers ``assignment_status='active'``
with ``stack_id`` NULL (the FK ``ON DELETE SET NULL``) — invisible to BOTH the
config renderer (which selects ``WHERE stack_id == stack.id``) and the Pending
inbox (``status='pending'``), so they silently stopped trading. Two guards:

* ``stacks._demote_stack_customers`` — a deprovisioned stack's customers become
  'pending' (visible in the inbox) instead of orphaned-active.
* ``distribution.pending_customers`` also surfaces any ``active`` + stack_id-NULL
  orphans (belt-and-suspenders for out-of-band cases).
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.models.audit import AuditLog
from app.services import distribution, stacks


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _DemoteDB:
    def __init__(self, customer_ids):
        self._ids = customer_ids
        self.added: list = []
        self.execute_stmts: list = []

    async def execute(self, stmt):
        self.execute_stmts.append(stmt)
        if len(self.execute_stmts) == 1:   # the select(Customer.id)
            return _Result(list(self._ids))
        return _Result([])                 # the update

    def add(self, obj):
        self.added.append(obj)


async def test_demote_stack_customers_demotes_and_audits():
    ids = [uuid.uuid4(), uuid.uuid4()]
    db = _DemoteDB(ids)
    n = await stacks._demote_stack_customers(db, uuid.uuid4(), uuid.uuid4())
    assert n == 2
    assert len(db.execute_stmts) == 2          # select + update
    # the UPDATE sets status='pending' and clears stack_id/server_id
    update_sql = str(db.execute_stmts[1]).lower()
    assert "update" in update_sql and "assignment_status" in update_sql
    audits = [a for a in db.added if isinstance(a, AuditLog)]
    assert len(audits) == 1
    assert audits[0].action == "stack.demote_customers"


async def test_demote_stack_customers_noop_when_empty():
    db = _DemoteDB([])
    n = await stacks._demote_stack_customers(db, uuid.uuid4(), uuid.uuid4())
    assert n == 0
    assert len(db.execute_stmts) == 1          # only the select; no update
    assert db.added == []


class _PendingDB:
    def __init__(self, rows):
        self._rows = rows
        self.last_stmt = None

    async def execute(self, stmt):
        self.last_stmt = stmt
        return _Result(self._rows)


async def test_pending_customers_surfaces_active_orphans():
    rows = [SimpleNamespace(id=uuid.uuid4())]
    db = _PendingDB(rows)
    out = await distribution.pending_customers(db)
    assert out == rows
    sql = str(db.last_stmt).lower()
    assert "assignment_status" in sql        # the pending branch
    assert "stack_id is null" in sql         # the orphan branch
