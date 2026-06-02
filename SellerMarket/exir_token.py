"""Exir / Rayan HamAfza per-request ``X-App-N`` signature (bot-side copy).

The bot package is a FLAT layout (the Dockerfile does ``COPY *.py ./``), so this
lives as a top-level module rather than in a sub-package. It is a verbatim port
of the mgmt UI's ``app/services/brokers/exir_token.py`` so both sides compute the
identical signature; keep them in sync.

Confirmed live (Phase-0 spike against khobregan): the token is computed over the
FULL path INCLUDING the query string, on a UTC clock, and changes every second —
so it must be recomputed immediately before each request. The computation is pure
arithmetic (no I/O), which keeps it safe on the locust head-of-queue hot path.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone


def build_app_n(nt: str, path_with_query: str, now: datetime | None = None) -> str:
    """Return the ``X-App-N`` header value for ``path_with_query``.

    Port of ``ExirTokenCrypto.BuildAppNToken``. ``now`` defaults to UTC now;
    pass an explicit value only for tests.
    """
    now = now or datetime.now(timezone.utc)
    text = nt[2:]
    if len(text) - 5 <= 0:
        raise ValueError(f"nt too short for X-App-N: len(nt)={len(nt)}")
    char_sum = sum(ord(c) for c in path_with_query)
    t = 3600 * now.hour + 60 * now.minute + now.second
    idx = abs(t % (len(text) - 5) - int(nt[0:2]))
    return f"{int(text[idx:idx + 5]) * t * char_sum}.{t * char_sum}"


def make_signer(nt: str, path_with_query: str):
    """Return a zero-I/O callable that yields a fresh ``{'X-App-N': ...}`` dict.

    Bakes the per-call-constant inputs (``text``/``char_sum``/``n0``) into a
    closure so the hot path only does the time read + a few arithmetic ops. Used
    by the locust ``place_order`` task: ephoenix's signer is ``None`` (static
    Bearer header), Exir's recomputes the second-granular signature per request.
    """
    text = nt[2:]
    if len(text) - 5 <= 0:
        raise ValueError(f"nt too short for X-App-N: len(nt)={len(nt)}")
    n0 = int(nt[0:2])
    char_sum = sum(ord(c) for c in path_with_query)
    span = len(text) - 5

    def _sign() -> dict[str, str]:
        now = datetime.now(timezone.utc)
        t = 3600 * now.hour + 60 * now.minute + now.second
        idx = abs(t % span - n0)
        return {"X-App-N": f"{int(text[idx:idx + 5]) * t * char_sum}.{t * char_sum}"}

    return _sign


def pw_fingerprint(password: str) -> str:
    """Stable non-secret cache-key component (sha256 hexdigest, first 16)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]
