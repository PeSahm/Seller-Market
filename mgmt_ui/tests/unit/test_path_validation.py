"""Unit tests for the SFTP path-validation guard.

The guard is the safety net that prevents the mgmt UI from clobbering files
outside each server's ``base_dir`` — most importantly the existing
``/root/seller-market/config.ini`` deployment.
"""

from __future__ import annotations

import pytest

from app.services.ssh.exceptions import PathOutOfScopeError
from app.services.ssh.sftp import _assert_path_in_scope


class _FakeServer:
    base_dir = "/root/seller-market/agents"


def test_allows_path_under_base_dir() -> None:
    _assert_path_in_scope(_FakeServer(), "/root/seller-market/agents/1/config.ini")


def test_allows_base_dir_itself() -> None:
    _assert_path_in_scope(_FakeServer(), "/root/seller-market/agents")


def test_rejects_root_level_config() -> None:
    """The mgmt UI MUST NOT touch the existing /root/seller-market/config.ini."""
    with pytest.raises(PathOutOfScopeError):
        _assert_path_in_scope(_FakeServer(), "/root/seller-market/config.ini")


def test_rejects_etc_passwd() -> None:
    with pytest.raises(PathOutOfScopeError):
        _assert_path_in_scope(_FakeServer(), "/etc/passwd")


def test_rejects_dotdot_traversal() -> None:
    with pytest.raises(PathOutOfScopeError):
        _assert_path_in_scope(
            _FakeServer(), "/root/seller-market/agents/../../etc/shadow"
        )


def test_rejects_sibling_dir_with_same_prefix() -> None:
    """'/root/seller-market/agents-evil' must NOT match '/root/seller-market/agents'."""
    with pytest.raises(PathOutOfScopeError):
        _assert_path_in_scope(_FakeServer(), "/root/seller-market/agents-evil/x")
