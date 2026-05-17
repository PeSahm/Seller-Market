"""Unit tests for :func:`app.services.audit.redact_payload` (Phase 9).

The redactor is the last-mile guard that stops a cleartext secret in an
``audit_log.before_json`` / ``after_json`` payload from being shipped to
the UI. Producing services SHOULD never write a secret into the payload
in the first place — but a regression somewhere upstream (e.g. a
scheduler-job upsert that accidentally serialises the whole upsert form
including a ``password`` field) would silently leak otherwise. The
redactor takes the belt-and-braces approach: walk every payload that
crosses the service-layer boundary and replace anything matching the
secret-key pattern with ``"***"``.

We pin seven behaviours here:

* Flat-dict redaction at depth 1.
* Nested-dict redaction at depth 2+.
* List-of-dicts redaction (positional walk).
* Case-insensitive key matching (``"Password"``, ``"API_KEY"``,
  ``"raw_PEM"``).
* ``None`` input -> ``None`` output (the JSONB column is nullable).
* Empty dict -> empty dict, **not** ``None`` (an explicit
  ``before_json={}`` is meaningful and must round-trip).
* Input is NOT mutated. This is critical: SQLAlchemy may reuse a JSONB
  column's dict on commit, so an in-place redaction would persist the
  sentinel back to the database the next time the row is flushed.
"""

from __future__ import annotations

from copy import deepcopy

from app.services.audit import redact_payload


# ---------------------------------------------------------------------------
# 1. Flat redactable key
# ---------------------------------------------------------------------------


def test_redact_flat_dict_password() -> None:
    """``{"password": "x"}`` -> ``{"password": "***"}``; other keys pass through."""
    payload = {"foo": 1, "password": "x"}
    out = redact_payload(payload)
    assert out == {"foo": 1, "password": "***"}


# ---------------------------------------------------------------------------
# 2. Nested redactable key
# ---------------------------------------------------------------------------


def test_redact_nested_dict_api_key() -> None:
    """Redaction recurses into nested dicts; sibling keys are untouched."""
    payload = {"creds": {"api_key": "abc", "user": "u"}}
    out = redact_payload(payload)
    assert out == {"creds": {"api_key": "***", "user": "u"}}


# ---------------------------------------------------------------------------
# 3. List of dicts
# ---------------------------------------------------------------------------


def test_redact_list_of_dicts() -> None:
    """Lists are walked element-wise; only dict items get redaction."""
    payload = {"keys": [{"private_key": "x"}, {"id": 1}]}
    out = redact_payload(payload)
    assert out == {"keys": [{"private_key": "***"}, {"id": 1}]}


# ---------------------------------------------------------------------------
# 4. Case-insensitive key matching across all redactable substrings
# ---------------------------------------------------------------------------


def test_redact_case_insensitive_keys() -> None:
    """``"Password"`` / ``"API_KEY"`` / ``"raw_PEM"`` all hit the redactor.

    Pins the IGNORECASE flag on :data:`_REDACT_KEY_PATTERN`. Without it,
    a producing service that camelCased a key would slip a cleartext
    secret past the guard.
    """
    payload = {
        "Password": "p1",
        "API_KEY": "k1",
        "raw_PEM": "-----BEGIN RSA-----",
        "fernet_KEY_PART1": "fk1",
        "Secret_Token": "st1",
    }
    out = redact_payload(payload)
    assert out == {
        "Password": "***",
        "API_KEY": "***",
        "raw_PEM": "***",
        "fernet_KEY_PART1": "***",
        "Secret_Token": "***",
    }


# ---------------------------------------------------------------------------
# 5. None -> None
# ---------------------------------------------------------------------------


def test_redact_none_returns_none() -> None:
    """``None`` payload (nullable JSONB column) round-trips as ``None``."""
    assert redact_payload(None) is None


# ---------------------------------------------------------------------------
# 6. Empty dict -> empty dict (NOT None)
# ---------------------------------------------------------------------------


def test_redact_empty_dict_returns_empty_dict() -> None:
    """An explicit empty payload must NOT be coerced to ``None``.

    ``before_json={}`` is semantically distinct from ``before_json=NULL``
    — the former means "we wrote a payload, it happened to be empty"
    (e.g. a no-op upsert with no diff) and we want the UI to render
    "no changes" rather than "no record".
    """
    out = redact_payload({})
    assert out == {}
    # And specifically not None:
    assert out is not None


# ---------------------------------------------------------------------------
# 7. Input is NOT mutated
# ---------------------------------------------------------------------------


def test_redact_does_not_mutate_input() -> None:
    """The original dict (and any nested dict/list) is untouched.

    This matters because SQLAlchemy may reuse a JSONB column's dict on
    commit; an in-place redaction would persist ``"***"`` back to the
    database the next time the row is flushed. We deepcopy the original
    up front and compare against the result of the call.
    """
    payload = {
        "user": "alice",
        "password": "secret-123",
        "nested": {
            "api_key": "abc-xyz",
            "stable": "ok",
        },
        "keys": [
            {"private_key": "pk1"},
            {"id": 1},
        ],
    }
    original_snapshot = deepcopy(payload)

    redacted = redact_payload(payload)

    # The original is byte-for-byte unchanged.
    assert payload == original_snapshot
    # And the redacted result is a different object at every level so
    # subsequent mutation of the result can't accidentally bleed back.
    assert redacted is not payload
    assert redacted["nested"] is not payload["nested"]
    assert redacted["keys"] is not payload["keys"]
    assert redacted["keys"][0] is not payload["keys"][0]
    # And the result is actually redacted:
    assert redacted["password"] == "***"
    assert redacted["nested"]["api_key"] == "***"
    assert redacted["keys"][0]["private_key"] == "***"
