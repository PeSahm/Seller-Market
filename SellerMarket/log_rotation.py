"""Rotate-then-truncate for the bot's bind-mounted log files.

Why this exists: ``locustfile_new.py`` (and ``cache_warmup.py``) used to blindly
TRUNCATE their log file at import/startup, so a later manual run destroyed the
previous run's evidence (2026-06-10 incident: the morning run's log was wiped
by a 10:18 manual re-run before it could be investigated). Now the previous
content is archived — gzipped, complete — under ``logs/`` first.

Hard constraints encoded here:

* ``trading_bot.log`` / ``cache_warmup.log`` are single-FILE bind mounts in the
  stack compose (``./trading_bot.log:/app/trading_bot.log``). The file must be
  truncated IN PLACE — never ``os.rename``/``os.replace`` it: a rename gives
  the container a new inode while the host keeps watching the old one, and the
  bind mount silently goes dead. ``logs/`` is a DIRECTORY mount, so new archive
  files created there appear on the host.

* locust runs with ``--processes 1`` which FORKS a worker — the locustfile is
  imported by master AND worker, so this rotation runs twice per run. The
  ``min_age_seconds`` guard makes the second import a no-op (the file is only
  seconds old by then); a bare size>0 check would let the worker archive the
  master's first lines as a junk file and truncate the live log.
"""
from __future__ import annotations

import glob
import gzip
import logging
import os
import re
import shutil
import time
from datetime import datetime

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 20  # gzipped archives per log name (env BOT_LOG_KEEP)

# Matches the same-second collision suffix ``-N`` before ``.log.gz`` (see
# ``rotate_and_truncate``: the first archive in a wall-clock second has no
# suffix, the next get ``-1``, ``-2``, … in creation order).
_COLLISION_SUFFIX = re.compile(r"-(\d+)\.log\.gz$")


def _keep_count(keep: int | None) -> int:
    if keep is not None:
        return max(1, int(keep))
    try:
        return max(1, int(os.environ.get("BOT_LOG_KEEP", DEFAULT_KEEP)))
    except (TypeError, ValueError):
        return DEFAULT_KEEP


def rotate_and_truncate(
    log_path: str,
    archive_dir: str = "logs",
    *,
    keep: int | None = None,
    min_age_seconds: float = 60.0,
) -> str | None:
    """Archive ``log_path``'s current content to ``archive_dir`` (gzipped),
    then truncate it in place. Returns the archive path, or ``None`` when
    nothing was rotated.

    Best-effort by design: this runs before logging is even configured, at the
    very start of a trading run — it must NEVER raise.
    """
    try:
        try:
            st = os.stat(log_path)
        except OSError:
            return None  # missing file — nothing to rotate
        if st.st_size == 0:
            return None  # already-truncated (e.g. the worker's second import)
        if time.time() - st.st_mtime < min_age_seconds:
            # Written seconds ago — this is the CURRENT run's file (the
            # master/worker double-import race); don't archive a live log.
            return None

        os.makedirs(archive_dir, exist_ok=True)

        stem = os.path.splitext(os.path.basename(log_path))[0]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(archive_dir, f"{stem}_{stamp}.log.gz")
        suffix = 0
        while os.path.exists(dest):
            suffix += 1
            dest = os.path.join(archive_dir, f"{stem}_{stamp}-{suffix}.log.gz")

        # Copy + truncate-in-place (NEVER rename — single-file bind mount).
        with open(log_path, "rb") as src, gzip.open(dest, "wb") as gz:
            shutil.copyfileobj(src, gz)
        with open(log_path, "w", encoding="utf-8"):
            pass  # truncate, same inode

        _prune(archive_dir, stem, _keep_count(keep))
        return dest
    except Exception:
        # Never let log housekeeping break a trading run.
        try:
            logger.warning("log rotation failed for %s", log_path, exc_info=True)
        except Exception:
            pass
        return None


def _archive_order_key(path: str) -> tuple[float, int]:
    """Sort key that orders archives oldest→newest DETERMINISTICALLY.

    Primary key is mtime. But the rotation stamp is second-granular, so several
    rotations in the same wall-clock second share an mtime — sorting by mtime
    alone leaves their order to the filesystem, which a fast runner resolves
    arbitrarily (the cause of the ``test_prune_keeps_only_n_archives`` flake).
    The collision suffix ``-N`` increments with creation order WITHIN a second
    (base = 0, then -1, -2, …), so it breaks the mtime tie in true chronological
    order. A plain lexical name sort can't do this — ``…061856-1.log.gz`` sorts
    BEFORE ``…061856.log.gz`` (``-`` < ``.``) yet was created later.
    """
    m = _COLLISION_SUFFIX.search(os.path.basename(path))
    return (os.path.getmtime(path), int(m.group(1)) if m else 0)


def _prune(archive_dir: str, stem: str, keep: int) -> None:
    """Delete the oldest archives beyond ``keep`` (deterministic on mtime ties —
    see :func:`_archive_order_key`)."""
    try:
        archives = sorted(
            glob.glob(os.path.join(archive_dir, f"{stem}_*.log.gz")),
            key=_archive_order_key,
        )
        for old in archives[:-keep] if keep else archives:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception:
        pass
