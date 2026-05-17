"""Path / filename safety tests for the janitor.

The janitor issues a literal remote ``rm -f`` against composed paths
that include attacker-controllable bits (the filename comes from
``find`` output on a server we control, but defence-in-depth: if a
shared-tenant box gets compromised we don't want the bot's
``order_results/`` directory to be a foothold for ``rm -rf /``).

We pin two layers:

1. :func:`_is_safe_filename` -- a strict whitelist regex on the bare
   basename. Shell metas, unicode, leading dots, ``..``, path
   separators, and trailing ``.bak`` are all rejected.
2. :func:`_assert_rm_target_in_scope` -- composes the full path with
   the stack's directory and refuses anything that doesn't have
   ``<stack_dir>/order_results/`` as a strict prefix. Also refuses
   obviously-wrong stack_dir values (``/``, ``/root``, ``/home``) and
   any path with a ``..`` segment.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _get_safe_filename():
    from app.services.janitor import _is_safe_filename
    return _is_safe_filename


def _get_scope_guard():
    from app.services.janitor import _assert_rm_target_in_scope
    return _assert_rm_target_in_scope


def _get_path_oos_error():
    from app.services.ssh.exceptions import PathOutOfScopeError
    return PathOutOfScopeError


# ---------------------------------------------------------------------------
# _is_safe_filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "4580090306_ayandeh_20260517_080000.json",
    "user_bbi_20260517_083001.json",
    "a.json",
    "A-B_C.json",
    "user-with-dash_karamad_20260517_120000.json",
])
def test_safe_filename_accepts_canonical_names(name):
    is_safe = _get_safe_filename()
    assert is_safe(name) is True, f"expected accept: {name!r}"


@pytest.mark.parametrize("name", [
    "",                                  # empty
    "..",                                # parent
    ".",                                 # current
    "./x.json",                          # path separator + leading dot
    "a/b.json",                          # forward slash
    "a\\b.json",                         # back slash
    ".hidden.json",                      # leading dot
    "x.json.bak",                        # not .json
    "x.JSON",                            # uppercase ext
    "x.txt",                             # wrong ext
    "x.json ",                           # trailing space
    "x;rm.json",                         # shell meta -- semicolon
    "x$(whoami).json",                   # shell meta -- command substitution
    "x`id`.json",                        # shell meta -- backtick
    "x|y.json",                          # shell meta -- pipe
    "x y.json",                          # space
    "café.json",                         # non-ASCII unicode
    "user_bbi_20260517_083001",          # no extension
])
def test_safe_filename_rejects_malformed(name):
    is_safe = _get_safe_filename()
    assert is_safe(name) is False, f"expected reject: {name!r}"


def test_safe_filename_rejects_overlong():
    is_safe = _get_safe_filename()
    # 257-char string (256 letters + .json would be 261 -- pick something
    # clearly over the 256-byte cap).
    name = "a" * 252 + ".json"  # 257 chars total
    assert is_safe(name) is False


def test_safe_filename_accepts_at_boundary():
    is_safe = _get_safe_filename()
    # 256-char total: 251 letters + ".json" (5 chars) = 256.
    name = "a" * 251 + ".json"
    assert is_safe(name) is True


# ---------------------------------------------------------------------------
# _assert_rm_target_in_scope
# ---------------------------------------------------------------------------


def _mk_stack(stack_dir: str):
    s = MagicMock()
    s.stack_dir = stack_dir
    return s


def test_scope_guard_passes_well_formed_path():
    guard = _get_scope_guard()
    stack = _mk_stack("/root/seller-market/agents/abc-123")
    # Should not raise.
    guard(
        stack,
        "/root/seller-market/agents/abc-123/order_results/x_bbi_20260101_000001.json",
    )


def test_scope_guard_rejects_root_stack_dir():
    guard = _get_scope_guard()
    err = _get_path_oos_error()
    stack = _mk_stack("/")
    with pytest.raises(err):
        guard(stack, "/order_results/x.json")


def test_scope_guard_rejects_root_user_stack_dir():
    guard = _get_scope_guard()
    err = _get_path_oos_error()
    stack = _mk_stack("/root")
    with pytest.raises(err):
        guard(stack, "/root/order_results/x.json")


def test_scope_guard_rejects_home_stack_dir():
    guard = _get_scope_guard()
    err = _get_path_oos_error()
    stack = _mk_stack("/home")
    with pytest.raises(err):
        guard(stack, "/home/order_results/x.json")


def test_scope_guard_rejects_empty_stack_dir():
    guard = _get_scope_guard()
    err = _get_path_oos_error()
    stack = _mk_stack("")
    with pytest.raises(err):
        guard(stack, "/order_results/x.json")


def test_scope_guard_rejects_dotdot_in_path():
    guard = _get_scope_guard()
    err = _get_path_oos_error()
    stack = _mk_stack("/root/seller-market/agents/abc")
    # Path passes the prefix check but contains a "..".
    full = "/root/seller-market/agents/abc/order_results/../../etc/passwd"
    with pytest.raises(err):
        guard(stack, full)


def test_scope_guard_rejects_path_outside_prefix():
    guard = _get_scope_guard()
    err = _get_path_oos_error()
    stack = _mk_stack("/root/seller-market/agents/abc")
    with pytest.raises(err):
        guard(stack, "/etc/passwd")


def test_scope_guard_rejects_sibling_directory():
    """Defensive: a path that's prefix-similar but not actually a child
    of ``<stack_dir>/order_results/`` must be refused."""
    guard = _get_scope_guard()
    err = _get_path_oos_error()
    stack = _mk_stack("/root/seller-market/agents/abc")
    # ``/.../abc-evil/`` would startswith ``/.../abc`` -- the trailing
    # slash in the expected prefix saves us. Make sure the guard relies
    # on that and rejects the evil sibling.
    with pytest.raises(err):
        guard(
            stack,
            "/root/seller-market/agents/abc-evil/order_results/x.json",
        )


def test_scope_guard_rejects_wrong_subdir():
    """Path inside the stack but in a sibling of order_results/ must be refused."""
    guard = _get_scope_guard()
    err = _get_path_oos_error()
    stack = _mk_stack("/root/seller-market/agents/abc")
    with pytest.raises(err):
        guard(stack, "/root/seller-market/agents/abc/secrets/x.json")


def test_scope_guard_tolerates_trailing_slash_in_stack_dir():
    """``stack_dir`` may or may not have a trailing slash; both compose
    to the same prefix."""
    guard = _get_scope_guard()
    stack = _mk_stack("/root/seller-market/agents/abc/")
    # Should not raise.
    guard(
        stack,
        "/root/seller-market/agents/abc/order_results/x.json",
    )
