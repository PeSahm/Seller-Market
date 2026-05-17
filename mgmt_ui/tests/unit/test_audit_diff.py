"""Unit tests for :func:`app.services.audit.diff_json` (Phase 9).

The diff function powers the per-row "before vs after" panel on
``/admin/audit/<id>``. It must:

* Run :func:`redact_payload` over both inputs BEFORE diffing — otherwise
  the operator's UI would surface the cleartext old and new values for a
  password rotation, which is exactly the leak the redactor exists to
  prevent.
* Produce flat dotted-path entries (``payload.nested.password``) so the
  template can render a simple table without recursion.
* Use bracket notation for list indices (``keys[0]``) so the path is
  unambiguous from a dict key whose name happens to be the digit
  ``"0"``.
* Be deterministic: identical inputs in different dict-insertion orders
  yield identical output. The UI relies on this for stable row order
  across successive page loads.
* Omit "equal" entries entirely — a diff with no changes is ``[]``, not
  a wall of ``change="unchanged"`` rows.

We pin eight cases below.
"""

from __future__ import annotations

from app.services.audit import diff_json


# ---------------------------------------------------------------------------
# 1. Identical inputs -> []
# ---------------------------------------------------------------------------


def test_diff_identical_returns_empty() -> None:
    """Two equal payloads produce no diff entries at all."""
    payload = {"a": 1, "b": {"c": 2}}
    assert diff_json(payload, payload) == []
    # Distinct-but-equal also returns empty (no identity assumption):
    assert diff_json({"a": 1, "b": {"c": 2}}, {"a": 1, "b": {"c": 2}}) == []


# ---------------------------------------------------------------------------
# 2. Added top-level key
# ---------------------------------------------------------------------------


def test_diff_added_top_level() -> None:
    """A key present in ``after`` but not ``before`` is reported as added."""
    before = {"a": 1}
    after = {"a": 1, "b": 2}
    entries = diff_json(before, after)
    assert len(entries) == 1
    e = entries[0]
    assert e.path == "b"
    assert e.before is None
    assert e.after == 2
    assert e.change == "added"


# ---------------------------------------------------------------------------
# 3. Removed top-level key
# ---------------------------------------------------------------------------


def test_diff_removed_top_level() -> None:
    """A key present in ``before`` but not ``after`` is reported as removed."""
    before = {"a": 1, "b": 2}
    after = {"a": 1}
    entries = diff_json(before, after)
    assert len(entries) == 1
    e = entries[0]
    assert e.path == "b"
    assert e.before == 2
    assert e.after is None
    assert e.change == "removed"


# ---------------------------------------------------------------------------
# 4. Changed top-level scalar
# ---------------------------------------------------------------------------


def test_diff_changed_top_level_scalar() -> None:
    """A key present on both sides with a different value -> ``change="changed"``."""
    before = {"name": "alice"}
    after = {"name": "bob"}
    entries = diff_json(before, after)
    assert len(entries) == 1
    e = entries[0]
    assert e.path == "name"
    assert e.before == "alice"
    assert e.after == "bob"
    assert e.change == "changed"


# ---------------------------------------------------------------------------
# 5. Nested dict change -> dotted path
# ---------------------------------------------------------------------------


def test_diff_nested_dotted_path() -> None:
    """Nested-dict changes produce ``"outer.inner"`` style paths.

    The whole "outer" key is unchanged at its own level — only the
    descended-into leaf differs — so the diff entry's path must point
    at the leaf, not the outer wrapper.
    """
    before = {"config": {"port": 80, "host": "x"}}
    after = {"config": {"port": 443, "host": "x"}}
    entries = diff_json(before, after)
    assert len(entries) == 1
    e = entries[0]
    assert e.path == "config.port"
    assert e.before == 80
    assert e.after == 443
    assert e.change == "changed"


# ---------------------------------------------------------------------------
# 6. List index change -> "key[0]" style path
# ---------------------------------------------------------------------------


def test_diff_list_index_change() -> None:
    """List elements use ``"key[idx]"`` notation, positional matching."""
    before = {"items": ["a", "b", "c"]}
    after = {"items": ["a", "X", "c"]}
    entries = diff_json(before, after)
    assert len(entries) == 1
    e = entries[0]
    assert e.path == "items[1]"
    assert e.before == "b"
    assert e.after == "X"
    assert e.change == "changed"


# ---------------------------------------------------------------------------
# 7. Redact runs BEFORE diff (password change -> no entry)
# ---------------------------------------------------------------------------


def test_diff_redacts_before_diffing() -> None:
    """A pure password rotation shows as no diff at all.

    The producing service replaced the cleartext password, but the
    redactor stamps both sides to ``"***"`` before the diff runs, so
    the comparison sees them as equal and emits no entry. This is the
    correct UX — the operator already knows from the ``action`` column
    that a password was set, and we MUST NOT ship the cleartext old or
    new value into the UI.

    The other (non-secret) field changed at the same time should still
    show up — proving that only the password value is suppressed, not
    the whole diff.
    """
    before = {"user": "alice", "password": "old-secret"}
    after = {"user": "alice", "password": "new-secret"}
    entries = diff_json(before, after)
    # Both passwords got stamped to "***" before the compare; no diff.
    assert entries == []

    # Sanity check: change a non-secret alongside the password and
    # confirm the non-secret change DOES surface (only the password
    # gets suppressed, the rest of the diff still works).
    before2 = {"user": "alice", "password": "old"}
    after2 = {"user": "bob", "password": "new"}
    entries2 = diff_json(before2, after2)
    assert len(entries2) == 1
    assert entries2[0].path == "user"
    assert entries2[0].before == "alice"
    assert entries2[0].after == "bob"


# ---------------------------------------------------------------------------
# 8. Deterministic ordering
# ---------------------------------------------------------------------------


def test_diff_deterministic_ordering() -> None:
    """Same inputs in different dict-insertion order yield the same list.

    Python dicts preserve insertion order, so a service that builds
    ``before`` / ``after`` payloads in different orders across two
    requests would naively produce different diff orders. The walk
    sorts keys at every depth so the output is stable — important for
    UI stability (the operator's eye expects the table to stop
    reshuffling between refreshes) and for test reproducibility.
    """
    before_a = {"a": 1, "b": 2, "c": 3}
    after_a = {"a": 10, "b": 20, "c": 30}

    # Same logical payload, different insertion order.
    before_b = {"c": 3, "a": 1, "b": 2}
    after_b = {"c": 30, "b": 20, "a": 10}

    entries_a = diff_json(before_a, after_a)
    entries_b = diff_json(before_b, after_b)

    # Convert to tuples for direct comparison (Pydantic models compare by
    # equality field-by-field).
    paths_a = [e.path for e in entries_a]
    paths_b = [e.path for e in entries_b]
    assert paths_a == paths_b == ["a", "b", "c"]

    # And the full entries are equal in shape too.
    assert [(e.path, e.before, e.after, e.change) for e in entries_a] == [
        (e.path, e.before, e.after, e.change) for e in entries_b
    ]


# ---------------------------------------------------------------------------
# Bonus: both None -> []
# ---------------------------------------------------------------------------


def test_diff_both_none_returns_empty() -> None:
    """Both inputs ``None`` (e.g. an ack with no payload) -> ``[]``."""
    assert diff_json(None, None) == []
