"""Read service for ``audit_log`` rows (Phase 9).

The audit log is written by every mutating service in the codebase
(:mod:`app.services.runs`, :mod:`app.services.scheduler_jobs`,
:mod:`app.services.stacks`, :mod:`app.services.health_signals`,
:mod:`app.services.settings_store`, ...). This module is the *read*
counterpart that powers ``/admin/audit`` — list + filter the feed, fetch
one row, and produce a redacted diff between the row's ``before_json``
and ``after_json`` payloads.

Three things live here:

* :func:`list_audit` / :func:`get_audit` — straightforward DB reads with
  the usual mix of optional filters (actor / action / target_type /
  target_id / time range / limit) ANDed together. Multi-select on
  ``action`` is supported via the ``actions`` iterable.
* :func:`redact_payload` — recursive walk that replaces secret-bearing
  values (``password``, ``api_key``, ``raw_pem``, ...) with the sentinel
  string ``"***"``. Run on *every* payload before it crosses the
  service-layer boundary, so the UI never ships a real secret even if a
  producing service accidentally left one in.
* :func:`diff_json` — flat-dotted-path diff between two JSON-ish dicts.
  Both sides are run through :func:`redact_payload` BEFORE the diff so a
  "password change" shows up as both sides being ``"***"`` (i.e. no
  entry). This is important: without it, the diff would surface the
  cleartext old vs new secret directly in the operator's UI.

The module deliberately has no SSH, no settings load, and no implicit
template rendering — just SQLAlchemy and Python stdlib. The acceptance
criterion is "``from app.services.audit import list_audit, diff_json,
redact_payload`` works without env vars set", which keeps the audit
read surface usable from a one-shot script or a notebook without the
full app boot.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.schemas.audit import AuditDiffEntry


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Match any key whose name contains one of the secret-bearing substrings,
# case-insensitive. Substring match (not whole-word) so we catch things
# like ``customer_password`` and ``broker_api_key`` without having to
# enumerate every variant the producing services might invent.
_REDACT_KEY_PATTERN = re.compile(
    r"(password|secret|token|raw_pem|private_key|fernet|api_key)",
    re.IGNORECASE,
)


def redact_payload(payload: dict | None) -> dict | None:
    """Return a new dict with secret-bearing values replaced by ``"***"``.

    Walks dicts and lists recursively. Any dict key whose name matches
    :data:`_REDACT_KEY_PATTERN` (case-insensitive substring) has its
    value replaced with the sentinel string ``"***"`` regardless of the
    value's original type. Non-secret keys recurse into their values so
    nested dicts/lists are redacted at every depth. Scalars
    (``str`` / ``int`` / ``bool`` / ``None`` / ...) pass through
    unchanged.

    ``None`` in -> ``None`` out. Empty dict in -> empty dict out (not
    ``None``).

    The input is **never** mutated. This matters because SQLAlchemy may
    reuse a JSONB column's dict on commit, so an in-place redaction
    would persist the sentinel back to the database the next time the
    row is flushed. We always return a fresh dict (and fresh lists for
    list values) so the caller can safely hand the result to a template
    while the ORM still owns the original.
    """
    if payload is None:
        return None
    return _redact_dict(payload)


def _redact_dict(d: dict) -> dict:
    """Recursive helper: redact one dict, returning a fresh dict."""
    out: dict = {}
    for k, v in d.items():
        # Keys are always treated as strings for the regex match;
        # producing services occasionally use non-str keys (uuid, int)
        # and we don't want a TypeError to leak the rest of the payload.
        if isinstance(k, str) and _REDACT_KEY_PATTERN.search(k):
            out[k] = "***"
        else:
            out[k] = _redact_value(v)
    return out


def _redact_value(v: object) -> object:
    """Recursive helper: redact one value (dict / list / scalar)."""
    if isinstance(v, dict):
        return _redact_dict(v)
    if isinstance(v, list):
        return [_redact_value(item) for item in v]
    return v


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def diff_json(
    before: dict | None,
    after: dict | None,
) -> list[AuditDiffEntry]:
    """Compute a flat, dotted-path diff between two JSON-ish payloads.

    Both inputs are first run through :func:`redact_payload` so we never
    compute a diff against an un-redacted value. This means that if the
    only field that changed is a redacted one (e.g. a password rotation)
    the result is an empty list — both sides are ``"***"`` so the diff
    sees them as equal. That's the correct UX: the operator already
    knows from the ``action`` column that a password was set; we should
    not be shipping the cleartext old and new values into the UI.

    Path conventions:

    * Nested dict keys are joined with ``"."``: ``"payload.password"``.
    * List indices use bracket notation: ``"keys[0].private_key"``.
    * Top-level keys are bare: ``"action"``.

    Diff semantics:

    * Key only on the ``after`` side -> ``change="added"`` with
      ``before=None``.
    * Key only on the ``before`` side -> ``change="removed"`` with
      ``after=None``.
    * Key on both sides with different values -> ``change="changed"``.
    * Equal values -> omitted entirely (no entry).

    Determinism: we walk keys in sorted order at every depth so two
    runs against the same inputs produce identical output regardless of
    dict insertion order. The UI's diff table assumes a stable row order
    so successive page loads don't reshuffle.

    If both inputs are ``None`` -> ``[]``. Either side being ``None``
    individually is treated as an empty dict for the purposes of the
    walk, so a row with only ``before`` populated yields a list of
    "removed" entries and vice versa.
    """
    before_r = redact_payload(before) if before is not None else None
    after_r = redact_payload(after) if after is not None else None

    if before_r is None and after_r is None:
        return []

    entries: list[AuditDiffEntry] = []
    _walk_diff(
        before_r if before_r is not None else {},
        after_r if after_r is not None else {},
        prefix="",
        out=entries,
    )
    return entries


# Sentinel used inside the walk to distinguish "key was absent" from
# "key was present with value None". A bare ``None`` wouldn't work
# because a payload may legitimately contain ``None`` values that
# should NOT be treated as "added"/"removed".
class _Missing:
    __slots__ = ()
    def __repr__(self) -> str:  # pragma: no cover - debug only
        return "<missing>"


_MISSING = _Missing()


def _walk_diff(
    before: object,
    after: object,
    *,
    prefix: str,
    out: list[AuditDiffEntry],
) -> None:
    """Recursive helper: walk two values in parallel, emitting entries.

    Called with ``prefix=""`` at the top level; recursive calls extend
    the prefix with ``"<key>"`` for dict descent or ``"[<idx>]"`` for
    list descent.

    We do NOT try to be clever about list matching — same index on both
    sides is compared, length mismatch surfaces as added/removed
    entries at the extra indices. This matches the rest of the codebase
    where lists in JSONB payloads are positional (e.g. a list of pinned
    server ids has stable order).
    """
    if isinstance(before, dict) and isinstance(after, dict):
        _walk_dicts(before, after, prefix=prefix, out=out)
        return

    if isinstance(before, list) and isinstance(after, list):
        _walk_lists(before, after, prefix=prefix, out=out)
        return

    # Mixed types (e.g. before was a dict, after is now a scalar) or two
    # scalars: just compare equality. We deliberately don't try to
    # "merge" a dict -> scalar transition into per-key removals; the
    # whole subtree changed shape so we report it as one "changed"
    # entry at this prefix.
    if before != after:
        out.append(
            AuditDiffEntry(
                path=prefix or "",
                before=before,
                after=after,
                change="changed",
            )
        )


def _walk_dicts(
    before: dict,
    after: dict,
    *,
    prefix: str,
    out: list[AuditDiffEntry],
) -> None:
    """Walk two dicts at the same depth, sorted by key for determinism."""
    keys = sorted(set(before.keys()) | set(after.keys()), key=str)
    for k in keys:
        path = f"{prefix}.{k}" if prefix else str(k)
        b_val = before.get(k, _MISSING)
        a_val = after.get(k, _MISSING)

        if b_val is _MISSING:
            # Key was added in `after`.
            out.append(
                AuditDiffEntry(
                    path=path,
                    before=None,
                    after=a_val,
                    change="added",
                )
            )
            continue
        if a_val is _MISSING:
            # Key was removed from `after`.
            out.append(
                AuditDiffEntry(
                    path=path,
                    before=b_val,
                    after=None,
                    change="removed",
                )
            )
            continue

        # Both sides have the key — recurse to compare values.
        _walk_diff(b_val, a_val, prefix=path, out=out)


def _walk_lists(
    before: list,
    after: list,
    *,
    prefix: str,
    out: list[AuditDiffEntry],
) -> None:
    """Walk two lists in parallel using positional matching by index."""
    longer = max(len(before), len(after))
    for i in range(longer):
        path = f"{prefix}[{i}]"
        if i >= len(before):
            out.append(
                AuditDiffEntry(
                    path=path,
                    before=None,
                    after=after[i],
                    change="added",
                )
            )
            continue
        if i >= len(after):
            out.append(
                AuditDiffEntry(
                    path=path,
                    before=before[i],
                    after=None,
                    change="removed",
                )
            )
            continue
        _walk_diff(before[i], after[i], prefix=path, out=out)


# ---------------------------------------------------------------------------
# DB reads
# ---------------------------------------------------------------------------


async def list_audit(
    db: AsyncSession,
    *,
    actor_id: Optional[UUID] = None,
    action: Optional[str] = None,
    actions: Optional[Iterable[str]] = None,
    action_contains: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 200,
) -> list[AuditLog]:
    """Filter audit rows on common dimensions; newest-first.

    Filter combinations are ANDed together. Each filter is optional.

    ``action`` and ``actions`` are mutually exclusive in practice
    (the multi-select UI never sends both), but if a caller passes
    both, ``actions`` wins — that matches the UI's behaviour where
    selecting a chip in the multi-select overrides any pre-existing
    single-value selection. An empty ``actions`` iterable is treated as
    "no filter" rather than "no rows", since an empty multi-select on
    the form means the user didn't pick anything.

    ``action_contains`` is a case-insensitive substring filter applied
    in SQL (``ILIKE %term%``) so the LIMIT runs AFTER the filter — a
    previous version of this code did the substring match in Python
    AFTER fetching the top N, which silently dropped matching rows
    that fell outside the cap window. The ``%`` and ``_`` metacharacters
    in the user input are escaped (with ``\\`` as the escape char) so a
    literal ``percent``-or-``underscore``-containing action name doesn't
    accidentally match unrelated rows. If both ``action_contains`` and
    one of ``action`` / ``actions`` are given, the substring filter is
    ANDed on top — the routes never send both today, but the contract
    is documented.

    ``limit`` defaults to 200 to match the UI's table page; callers
    paginating manually can override.
    """
    stmt = select(AuditLog).order_by(desc(AuditLog.ts)).limit(limit)

    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_user_id == actor_id)

    if actions is not None:
        actions_list = list(actions)
        if actions_list:
            stmt = stmt.where(AuditLog.action.in_(actions_list))
        # else: empty multi-select -> no filter on action at all
    elif action is not None:
        stmt = stmt.where(AuditLog.action == action)

    if action_contains:
        # Escape the LIKE wildcards (%, _) and the escape char (\) itself
        # so a user search like "user_id" doesn't behave as "user<any>id".
        escaped = (
            action_contains
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        stmt = stmt.where(AuditLog.action.ilike(f"%{escaped}%", escape="\\"))

    if target_type is not None:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if since is not None:
        stmt = stmt.where(AuditLog.ts >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.ts <= until)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_audit(db: AsyncSession, audit_id: UUID) -> Optional[AuditLog]:
    """Fetch one audit row by PK. ``None`` if missing."""
    return await db.get(AuditLog, audit_id)


__all__ = [
    "list_audit",
    "get_audit",
    "redact_payload",
    "diff_json",
    "_REDACT_KEY_PATTERN",
]
