"""Unit tests for the stack-health worker's compose-ps output parser.

The parser is the only pure function in :mod:`app.workers.stack_health`; the
remote-side SSH probe is exercised by integration tests in a later phase.

Why focus tests here? ``docker compose ps --format json`` is the contract
boundary between the mgmt UI and the docker compose runtime, and that runtime
has shipped at least three different shapes of JSON output across the v2.x
series (single object, JSON array of objects, JSON-Lines). The parser must
handle all three plus partial corruption.
"""

from __future__ import annotations

from app.workers.stack_health import _parse_compose_status


def test_parse_empty_returns_down() -> None:
    """No output at all means ``docker compose ps`` saw no containers."""
    assert _parse_compose_status("") == "down"
    assert _parse_compose_status("   \n  ") == "down"


def test_parse_single_running_returns_up() -> None:
    """Older docker-compose emits a single JSON object when only one container exists."""
    stdout = '{"Name": "agent_app", "State": "running", "Status": "Up 5 minutes"}'
    assert _parse_compose_status(stdout) == "up"


def test_parse_json_array_with_running_returns_up() -> None:
    """Some docker-compose versions wrap the output in a JSON array."""
    stdout = (
        '[{"Name": "agent_app", "State": "running"}, '
        '{"Name": "agent_db", "State": "running"}]'
    )
    assert _parse_compose_status(stdout) == "up"


def test_parse_jsonl_with_running_returns_up() -> None:
    """Newer docker-compose v2.x emits one JSON object per line (JSON Lines)."""
    stdout = (
        '{"Name": "agent_app", "State": "running"}\n'
        '{"Name": "agent_db", "State": "running"}\n'
    )
    assert _parse_compose_status(stdout) == "up"


def test_parse_all_exited_returns_down() -> None:
    """Every container exited cleanly => stack is down."""
    stdout = (
        '{"Name": "agent_app", "State": "exited"}\n'
        '{"Name": "agent_db", "State": "exited"}\n'
    )
    assert _parse_compose_status(stdout) == "down"


def test_parse_garbage_returns_down() -> None:
    """Unparseable output is treated as "down" — we'd rather mis-report a
    healthy stack than crash the worker loop."""
    assert _parse_compose_status("not json at all") == "down"
    assert _parse_compose_status("{broken json") == "down"


def test_parse_mixed_states_with_one_running_returns_up() -> None:
    """Any running container is enough to consider the stack 'up'.

    Real-world example: the app container is healthy but the (optional) OCR
    sidecar is starting up. We want the dashboard to say ``up``, not flap to
    ``down`` because of a benign sidecar restart.
    """
    stdout = (
        '{"Name": "agent_app", "State": "running"}\n'
        '{"Name": "agent_ocr", "State": "restarting"}\n'
    )
    assert _parse_compose_status(stdout) == "up"


def test_parse_jsonl_with_partial_corruption_returns_up() -> None:
    """A garbage line in the middle of JSON-Lines is skipped, not fatal."""
    stdout = (
        '{"Name": "agent_app", "State": "running"}\n'
        "<<< garbage line >>>\n"
        '{"Name": "agent_db", "State": "exited"}\n'
    )
    assert _parse_compose_status(stdout) == "up"


def test_parse_lowercase_state_key_returns_up() -> None:
    """Some docker-compose versions emit ``state`` (lowercase) instead of ``State``.

    The parser accepts both for robustness.
    """
    stdout = '{"name": "agent_app", "state": "Running"}'
    assert _parse_compose_status(stdout) == "up"


def test_parse_non_string_state_does_not_crash() -> None:
    """Non-string ``State`` values (numeric, null, etc.) must not crash the
    parser.

    Some docker compose versions / build modes have been observed to emit
    numeric or null states under edge conditions (e.g. partially-initialized
    containers). The parser must coerce them rather than letting an
    ``AttributeError`` from ``.lower()`` abort the entire tick.
    """
    payload = '[{"State": 1}, {"State": null}, {"State": "running"}]'
    assert _parse_compose_status(payload) == "up"


def test_parse_only_non_string_states_returns_down() -> None:
    """All non-string / non-running states still resolve to ``down`` cleanly."""
    payload = '[{"State": 1}, {"State": null}, {"State": {"nested": "obj"}}]'
    assert _parse_compose_status(payload) == "down"
