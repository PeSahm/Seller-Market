"""Bot runtime config — DB-pushed overrides for values that used to be hardcoded.

The mgmt UI renders a ``[runtime]`` section into the bot's ``config.ini`` (the
file already mounted single-file into the container) and pushes it to every
stack via the existing in-place SFTP write. This module reads that section so an
operator can change broker/market-data hosts, time windows, fees, etc. and have
ALL stacks pick it up within seconds — **no CI, no image rebuild, no container
recreate**. Before this existed, a value like the ephoenix market-data host
(``mdapi1`` -> ``marketdatagw``) was baked into the image and needed the whole
build+redeploy cycle to change.

Design:

* **Read-through with a hardcoded fallback** — every call site does
  ``runtime_config.get("ephoenix_md_host", "marketdatagw")``. With no
  ``[runtime]`` section every ``get`` misses and returns the fallback, so the
  bot behaves EXACTLY as today until a value is actually edited. That is what
  makes the rollout safe in every ordering (old image ignores the section; new
  image with no section == today).
* **Call-time, mtime+TTL cached** — long-running processes (the auto-sell
  monitor, the market-data sidecar) re-read within ``_TTL_S`` of a pushed
  change; per-run subprocesses (a scheduled trading run / cache-warmup) read
  fresh on first use. The TTL keeps the stat/parse cost off hot paths.
* **Sentinel/torn-write gate** — a value is adopted ONLY from a file that ends
  with ``CONFIG_END_SENTINEL`` (the same ``# auto-sell-config-end`` marker the
  auto-sell monitor trusts). The SFTP write is an in-place front-to-back
  truncate+rewrite, so a torn read is a PREFIX missing the sentinel; we keep the
  last-good snapshot and retry on the next poll once the write completes.
* **``%``-safe** — parsed with ``RawConfigParser`` (no ``%`` interpolation), so
  a URL or password containing ``%`` never raises.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import configparser
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Must match auto_sell_monitor.CONFIG_END_SENTINEL. Defined locally (not imported)
# to avoid a circular import: this low-level module is imported by broker_enum /
# rlc_price / captcha_utils, while auto_sell_monitor imports broker_adapters ->
# the adapters -> broker_enum, so importing it here would loop.
CONFIG_END_SENTINEL = "# auto-sell-config-end"

_SECTION = "runtime"
_TTL_S = 2.0  # max staleness for a long-running process after a pushed change
_TRUTHY = {"1", "true", "yes", "on"}

# Injected in tests; real monotonic clock otherwise.
_clock = time.monotonic

_lock = threading.Lock()
_state: dict = {
    "values": {},        # last-good parsed [runtime] dict (lowercased keys)
    "mtime": None,       # st_mtime_ns of the file when last adopted
    "checked_at": None,  # _clock() of the last stat (None == never)
}


def _config_path() -> str:
    """The config.ini path (env-overridable, matching bot_entrypoint/auto-sell)."""
    return os.environ.get("CONFIG_INI", "/app/config.ini")


def _trusted(text: str) -> bool:
    return text.rstrip().endswith(CONFIG_END_SENTINEL)


def _parse_runtime(text: str) -> dict[str, str]:
    """Parse the whole config.ini text and return the ``[runtime]`` section dict.

    RawConfigParser ⇒ no ``%`` interpolation (URLs / passwords with ``%`` are
    safe). Returns ``{}`` when there is no ``[runtime]`` section.
    """
    cp = configparser.RawConfigParser()
    cp.read_string(text)
    if not cp.has_section(_SECTION):
        return {}
    # The mgmt renderer ``%%``-escapes every value so the bot's OTHER readers,
    # which use an interpolating ConfigParser over the WHOLE file (e.g.
    # auto_sell_monitor.parse_auto_sell_targets), don't choke on a literal ``%``
    # (a URL ``%20``). RawConfigParser does no interpolation, so undo that escape
    # here to recover the literal value. A value with no ``%`` is unaffected.
    return {k: v.replace("%%", "%") for k, v in cp.items(_SECTION)}


def _snapshot() -> dict[str, str]:
    """Return the current ``[runtime]`` dict, refreshing from disk on mtime change
    at most once per ``_TTL_S``. Never raises — any error keeps the last-good."""
    now = _clock()
    with _lock:
        last = _state["checked_at"]
        if last is not None and (now - last) < _TTL_S:
            return _state["values"]

        path = _config_path()
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            _state["checked_at"] = now
            return _state["values"]

        if mtime == _state["mtime"]:
            _state["checked_at"] = now
            return _state["values"]

        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            _state["checked_at"] = now
            return _state["values"]

        # Torn write / pre-sentinel render → keep last-good, DON'T cache this
        # mtime, so the next poll re-reads and adopts once the write completes.
        if not _trusted(text):
            _state["checked_at"] = now
            return _state["values"]

        try:
            values = _parse_runtime(text)
        except configparser.Error:
            # Structurally broken (mid-write race that still ends in the
            # sentinel, or hand-edit) → keep last-good, retry next poll.
            _state["checked_at"] = now
            return _state["values"]

        _state["values"] = values
        _state["mtime"] = mtime
        _state["checked_at"] = now
        return values


def get(key: str, default: str = "") -> str:
    """Override for ``key`` from ``[runtime]``, else ``default``. Empty == absent."""
    v = _snapshot().get(key, "")
    return v if v != "" else default


def get_int(key: str, default: int) -> int:
    raw = get(key, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float) -> float:
    raw = get(key, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    raw = get(key, "")
    if raw == "":
        return default
    return raw.strip().lower() in _TRUTHY


def get_list(key: str, default: Optional[list[str]] = None) -> list[str]:
    """Comma/space-separated value → ordered list. Absent → ``default`` or ``[]``."""
    raw = get(key, "")
    if raw == "":
        return list(default) if default is not None else []
    return [p for p in raw.replace(",", " ").split() if p.strip()]


def drop_non_customer_sections(cp) -> None:
    """Remove non-account sections from a loaded ``ConfigParser`` in place.

    The mgmt renderer adds a global ``[runtime]`` section (endpoint/host
    overrides) alongside the per-customer sections. The bot's per-account
    iterators (``cache_warmup`` main loop, ``locustfile_new`` user-class build +
    summaries) assume EVERY ``config.sections()`` entry is a customer and read
    ``section['username']`` directly — a ``[runtime]`` section makes them
    ``KeyError``. Those modules read the runtime OVERRIDES via this module's
    :func:`get` (a separate file read), NOT the ``ConfigParser`` object, so it is
    safe to drop any section lacking a ``username`` (the global ``[runtime]``
    block, and defensively any future non-account section) right after load.
    """
    for name in list(cp.sections()):
        if not cp.has_option(name, "username"):
            cp.remove_section(name)


def snapshot() -> dict[str, str]:
    """A copy of the current ``[runtime]`` dict (debugging / tests)."""
    return dict(_snapshot())


def reset_cache() -> None:
    """Drop the cached snapshot (tests)."""
    with _lock:
        _state["values"] = {}
        _state["mtime"] = None
        _state["checked_at"] = None


__all__ = [
    "get", "get_int", "get_float", "get_bool", "get_list",
    "snapshot", "reset_cache", "drop_non_customer_sections", "CONFIG_END_SENTINEL",
]
