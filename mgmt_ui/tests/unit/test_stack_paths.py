"""Unit tests for the per-stack path-validation guard.

The guard is the per-stack twin of the SFTP layer's per-server guard. It
exists for defence-in-depth: the per-server guard prevents writes outside
``server.base_dir``, but the stack service additionally narrows the allowed
prefix to ``stack.stack_dir`` so a misaligned ``stack_dir`` (e.g. from a
buggy migration) can't cause us to clobber another agent's directory.

Importantly these tests must not touch SSH — the SSH layer already has its
own dedicated tests in ``test_path_validation.py`` and ``test_ssh_atomic_write.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.ssh.exceptions import PathOutOfScopeError
from app.services.stacks import _assert_stack_path_in_scope


# A representative agent UUID used in the test fixtures. We pick one at
# module load and reuse it so each test reads cleanly without inline UUIDs.
_AGENT_ID = uuid4()


class _FakeStack:
    """Minimal stand-in for an ``AgentStack`` ORM row.

    The guard only reads ``stack_dir``, so we don't need to bother with the
    other columns. Using a plain class (rather than dataclasses) keeps the
    fixture noise-free.
    """

    def __init__(self, stack_dir: str) -> None:
        self.stack_dir = stack_dir


def _stack_under_agents() -> _FakeStack:
    """A typical stack: ``<agents>/<agent_id>``."""
    return _FakeStack(stack_dir=f"/root/seller-market/agents/{_AGENT_ID}")


# ---------------------------------------------------------------------------
# Required tests per the Phase 3 spec
# ---------------------------------------------------------------------------


def test_stack_path_guard_rejects_root_level() -> None:
    """The legacy ``/root/seller-market/config.ini`` MUST stay untouchable.

    A stack pointed at ``/root/seller-market/agents/<uuid>`` must reject
    any operation on the existing root-level config file — the trading bot
    runs against that file and the mgmt UI must never clobber it.
    """
    stack = _stack_under_agents()
    with pytest.raises(PathOutOfScopeError):
        _assert_stack_path_in_scope(stack, "/root/seller-market/config.ini")


def test_stack_path_guard_allows_inside_stack() -> None:
    """Paths strictly under the stack dir are allowed."""
    stack = _stack_under_agents()
    # Should not raise.
    _assert_stack_path_in_scope(
        stack, f"/root/seller-market/agents/{_AGENT_ID}/config.ini"
    )


def test_stack_path_guard_rejects_dotdot() -> None:
    """``..`` traversal must not climb out of the stack scope.

    posixpath.normpath collapses the ``..`` before we check the prefix, so
    ``/root/seller-market/agents/<uuid>/../../etc/shadow`` resolves to
    ``/root/seller-market/etc/shadow`` — outside the stack dir, hence
    rejected.
    """
    stack = _stack_under_agents()
    with pytest.raises(PathOutOfScopeError):
        _assert_stack_path_in_scope(
            stack,
            f"/root/seller-market/agents/{_AGENT_ID}/../../etc/shadow",
        )


def test_stack_path_guard_rejects_sibling_prefix() -> None:
    """``<stack_dir>-evil`` must NOT match ``<stack_dir>`` by simple prefix.

    Without the trailing-slash dance in the guard, a naive
    ``startswith(base)`` check would accept paths that merely begin with the
    same characters as ``stack_dir`` — e.g. an attacker who could control a
    sibling directory named ``<uuid>-evil`` would be able to write under it.
    The guard mandates ``base + '/'`` exactly to close this off.
    """
    stack = _stack_under_agents()
    evil_path = f"/root/seller-market/agents/{_AGENT_ID}-evil/x"
    with pytest.raises(PathOutOfScopeError):
        _assert_stack_path_in_scope(stack, evil_path)


# ---------------------------------------------------------------------------
# Extra coverage for malformed stack_dir values
# ---------------------------------------------------------------------------


def test_stack_path_guard_rejects_root_stack_dir() -> None:
    """A stack row with ``stack_dir='/'`` must be refused outright.

    Same reasoning as the per-server guard: ``normpath('/').rstrip('/')``
    collapses to an empty string and ``'' + '/'`` is a prefix of every
    absolute path. Refusing at the guard means a manual DB edit that sets
    ``stack_dir='/'`` cannot lead to ``rm -rf /`` later.
    """
    stack = _FakeStack(stack_dir="/")
    with pytest.raises(PathOutOfScopeError):
        _assert_stack_path_in_scope(stack, "/etc/passwd")


def test_stack_path_guard_rejects_empty_stack_dir() -> None:
    """A blank ``stack_dir`` is not a valid scope and must be refused."""
    stack = _FakeStack(stack_dir="")
    with pytest.raises(PathOutOfScopeError):
        _assert_stack_path_in_scope(stack, "/anything")
