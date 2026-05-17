"""Pure-logic tests for :func:`app.services.run_executor._build_command`.

The executor itself orchestrates SSH + the scheduler-snapshot context +
the run-locks ledger, so a full end-to-end test would need a real
``paramiko`` session. The command builder, by contrast, is a pure
string-producing function we can pin exactly with no I/O.

Why test a private (underscore-prefixed) helper directly?

* It's the contract-pinning surface for what gets sent to ``docker
  exec`` on the agent box — a silent regression here would either
  fail-open (e.g. dropped ``--processes`` flag) or fail-closed (e.g.
  un-quoted container name letting a future tenant pun on shell
  metacharacters in their compose project).
* The integration / end-to-end test pyramid layers above this won't
  catch flag-level changes — they'd just observe "command succeeded"
  / "command failed".

Five branches:

1. ``cache_warmup`` produces the documented ``docker exec ... python
   cache_warmup.py`` form with the container name shell-quoted.
2. ``run_trading`` with ``locust_cfg=None`` uses the Phase-3 fleet
   defaults — and crucially omits ``--processes 1`` (Locust's default,
   keeps the command tidy and avoids a flag drift if Locust ever
   changes its default).
3. ``run_trading`` with a custom :class:`LocustConfig` injects every
   field verbatim and emits ``--processes N`` when N != 1.
4. ``run_trading`` with ``processes=1`` (whether from the default or
   an explicit row) does NOT emit ``--processes 1``.
5. An unknown ``job_name`` raises :class:`ValueError`.
"""

from __future__ import annotations

import shlex
from types import SimpleNamespace

import pytest

from app.services.run_executor import _build_command


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_stack(compose_project: str = "sm-agent-abc") -> SimpleNamespace:
    """Minimal stack stand-in — _build_command only reads ``compose_project``."""
    return SimpleNamespace(compose_project=compose_project)


def _fake_locust_cfg(
    *,
    users: int,
    spawn_rate: int,
    run_time: str,
    host: str,
    processes: int,
) -> SimpleNamespace:
    """:class:`LocustConfig` stand-in for the renderer projection."""
    return SimpleNamespace(
        users=users,
        spawn_rate=spawn_rate,
        run_time=run_time,
        host=host,
        processes=processes,
    )


# ---------------------------------------------------------------------------
# 1. test_cache_warmup_command
# ---------------------------------------------------------------------------


def test_cache_warmup_command() -> None:
    """``cache_warmup`` → exactly ``docker exec '<container>' python cache_warmup.py``.

    Pin the full string (including the single-quoted container) so a
    future "let's drop the shlex.quote" tweak fails loudly. The
    quoting matters because the compose project is operator-supplied
    free text and a shell-metacharacter in a future tenant's name
    would otherwise punt straight to the remote shell.
    """
    stack = _fake_stack(compose_project="sm-agent-abc")
    container = "sm-agent-abc-bot"
    cmd = _build_command("cache_warmup", stack, None)

    assert cmd == f"docker exec {shlex.quote(container)} python cache_warmup.py"
    # And explicitly: the container is quoted.
    assert f"'{container}'" in cmd or shlex.quote(container) == container


# ---------------------------------------------------------------------------
# 2. test_run_trading_command_with_defaults
# ---------------------------------------------------------------------------


def test_run_trading_command_with_defaults() -> None:
    """``locust_cfg=None`` → Phase-3 fleet defaults.

    The defaults are:

    * users = 10
    * spawn_rate = 10
    * run_time = "120s"
    * host = "https://abc.com"
    * processes = 1 (omitted from the command — Locust's default)

    Each flag value is checked individually so a partial regression
    (e.g. spawn_rate drift) names the exact field in the failure.
    """
    stack = _fake_stack(compose_project="sm-agent-abc")
    cmd = _build_command("run_trading", stack, None)

    assert "docker exec" in cmd
    assert shlex.quote("sm-agent-abc-bot") in cmd
    assert "locust" in cmd
    assert "-f locustfile_new.py" in cmd
    assert "--headless" in cmd
    assert "--users 10" in cmd
    assert "--spawn-rate 10" in cmd
    assert "--run-time 120s" in cmd
    assert "--host https://abc.com" in cmd
    # Critical: --processes 1 is omitted to keep the command tidy.
    assert "--processes" not in cmd


# ---------------------------------------------------------------------------
# 3. test_run_trading_command_with_custom_locust_cfg
# ---------------------------------------------------------------------------


def test_run_trading_command_with_custom_locust_cfg() -> None:
    """Custom :class:`LocustConfig` row → every field appears verbatim.

    With ``processes != 1`` the ``--processes`` flag MUST appear (it's
    what enables Locust's multi-process worker mode for heavier load
    tests).
    """
    stack = _fake_stack(compose_project="sm-agent-xyz")
    locust = _fake_locust_cfg(
        users=50,
        spawn_rate=5,
        run_time="300s",
        host="https://broker.example",
        processes=4,
    )

    cmd = _build_command("run_trading", stack, locust)

    assert shlex.quote("sm-agent-xyz-bot") in cmd
    assert "--users 50" in cmd
    assert "--spawn-rate 5" in cmd
    assert "--run-time 300s" in cmd
    assert "--host https://broker.example" in cmd
    assert "--processes 4" in cmd
    # And nothing leaks from the default fallback (10 users is the
    # Phase-3 default — must not appear here).
    assert "--users 10" not in cmd


# ---------------------------------------------------------------------------
# 4. test_run_trading_command_processes_one_is_omitted
# ---------------------------------------------------------------------------


def test_run_trading_command_processes_one_is_omitted() -> None:
    """Explicit ``processes=1`` on the row still omits the flag.

    Locust treats omission of ``--processes`` as "single process"; we
    don't emit ``--processes 1`` explicitly so the command stays
    visually clean in the operator's run-history UI. The branch that
    enforces this is ``if processes and processes != 1:`` in
    :func:`_build_command`.
    """
    stack = _fake_stack(compose_project="sm-agent-abc")
    locust = _fake_locust_cfg(
        users=25,
        spawn_rate=3,
        run_time="60s",
        host="https://broker.example",
        processes=1,
    )

    cmd = _build_command("run_trading", stack, locust)

    assert "--users 25" in cmd
    # The custom fields land...
    assert "--spawn-rate 3" in cmd
    # ...but processes=1 is suppressed.
    assert "--processes" not in cmd
    assert "--processes 1" not in cmd


# ---------------------------------------------------------------------------
# 5. test_unknown_job_name_raises_value_error
# ---------------------------------------------------------------------------


def test_unknown_job_name_raises_value_error() -> None:
    """An unrecognised ``job_name`` is a ValueError, not a silent fallthrough.

    The HTTP/route layer uses a ``Literal`` to fence this off, but the
    service-level check defends against callers that bypass the
    schema (e.g. a future bulk-retry script that constructs the call
    directly).
    """
    stack = _fake_stack()
    with pytest.raises(ValueError, match="unknown job_name"):
        _build_command("invalid_job", stack, None)
