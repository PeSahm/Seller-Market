"""Pydantic schemas for run-lifecycle endpoints (Phase 6).

The ``runs`` table records every invocation of one of the bot's two jobs
(``cache_warmup`` / ``run_trading``) — whether it was fired by the
in-container scheduler or kicked off manually from the mgmt UI. This
module defines the small write-side payload the route layer accepts for
"start a manual run" and the read-side projection the UI / API render
back out, plus the three ``Literal`` types that mirror the matching
``SAEnum`` columns on :class:`app.models.runs.Run`.

We deliberately do NOT expose the ``log_blob_ref`` / ``log_blob_sha256``
columns in :class:`RunOut`. Those are server-side bookkeeping for
:func:`app.services.runs.finalize_run` / :func:`app.services.runs.read_run_log`
and the API streams the captured log through a dedicated endpoint instead
of leaking the on-disk path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

# Mirror the SAEnums on app.models.runs so a typo at the HTTP boundary
# turns into a clear 422 instead of an opaque IntegrityError further down.
JobName = Literal["cache_warmup", "run_trading"]
RunStatus = Literal["running", "success", "failed", "killed"]
RunTrigger = Literal["scheduled", "manual", "api", "retry"]


class RunStartRequest(BaseModel):
    """Body for ``POST /runs`` — kick off a manual run.

    ``trigger`` defaults to ``"manual"`` because the only path that
    accepts this payload is the operator-facing one. The scheduler /
    retry paths build the :class:`app.models.runs.Run` row directly and
    pass their own trigger string.
    """

    job_name: JobName
    trigger: RunTrigger = "manual"


class RunOut(BaseModel):
    """Outbound projection of a :class:`app.models.runs.Run` row.

    ``duration_seconds`` is computed from ``finished_at - started_at``
    and is ``None`` while the run is still ``running``. We populate it
    here (rather than on the model) so the column stays unambiguous: the
    DB stores timestamps, the API renders a derived float.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    stack_id: UUID
    agent_id: UUID
    job_name: str
    trigger: str
    started_at: datetime
    finished_at: Optional[datetime]
    status: str
    exit_code: Optional[int]
    duration_seconds: Optional[float] = None

    @classmethod
    def from_orm_row(cls, run) -> "RunOut":
        """Build a :class:`RunOut` from a :class:`app.models.runs.Run` row.

        We can't use plain ``model_validate(run)`` because Pydantic
        wouldn't know to compute ``duration_seconds`` from the two
        timestamps. The classmethod keeps the derivation in one place so
        routers don't have to repeat the ``(finished - started)`` math.
        """
        duration: Optional[float] = None
        if run.finished_at is not None and run.started_at is not None:
            duration = (run.finished_at - run.started_at).total_seconds()
        return cls(
            id=run.id,
            stack_id=run.stack_id,
            agent_id=run.agent_id,
            job_name=run.job_name,
            trigger=run.trigger,
            started_at=run.started_at,
            finished_at=run.finished_at,
            status=run.status,
            exit_code=run.exit_code,
            duration_seconds=duration,
        )


__all__ = [
    "JobName",
    "RunStatus",
    "RunTrigger",
    "RunStartRequest",
    "RunOut",
]
