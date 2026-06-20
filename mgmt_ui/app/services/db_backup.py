"""mgmt DB backup/restore core: frequent dump of the main -> warm spare + manifest.

Two consumers share this module (issue #156):

* The **cron** (runs on the spare host every 5-15 min via
  ``python -m app.services.db_backup``): ``pg_dump`` the main DB to a
  timestamped file, ``pg_restore`` it into the local ``postgres:18`` spare,
  append a JSON manifest entry, prune to the newest ``KEEP`` dumps.
* The **DB-down recovery console** (``app/routers/recovery.py``): reads the
  manifest with :func:`load_manifest` (works with NO database) and restores a
  chosen dump into the spare with :func:`restore_dump` (the "one-click restore").

The dump files double as rolling backups. PG18 client tools are REQUIRED
(``pg_dump``/``pg_restore`` must be >= the PG18 server).

The pure helpers (manifest / prune / sha256) are unit-tested; the
``pg_dump``/``pg_restore`` calls are injected so orchestration is testable
without a live database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("db_backup")

MANIFEST_NAME = "manifest.json"
_DUMP_SUFFIX = ".dump"

# Injectable runner types: (source, dest) -> None, raising on failure.
DumpFn = Callable[[str, str], None]
RestoreFn = Callable[[str, str], None]


def sha256_file(path: str | os.PathLike, _chunk: int = 1 << 20) -> str:
    """Streaming SHA-256 of a file (chunked so a large dump never loads whole)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_chunk), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest(manifest_path: str | os.PathLike) -> list[dict]:
    """Return the manifest as a list of entries; ``[]`` if missing/garbled.

    Never raises — the recovery console must read this even when the rest of
    the world (including the database) is on fire.
    """
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def save_manifest(manifest_path: str | os.PathLike, entries: list[dict]) -> None:
    """Atomically write the manifest (tmp + ``os.replace``)."""
    p = Path(manifest_path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def append_manifest(manifest_path: str | os.PathLike, entry: dict, keep: int) -> list[dict]:
    """Append ``entry`` (newest last), trim to the newest ``keep``, persist."""
    entries = load_manifest(manifest_path)
    entries.append(entry)
    if keep > 0 and len(entries) > keep:
        entries = entries[-keep:]
    save_manifest(manifest_path, entries)
    return entries


def prune_dumps(dump_dir: str | os.PathLike, keep: int) -> list[str]:
    """Keep the newest ``keep`` ``*.dump`` files; delete the rest. Return deleted paths.

    Tie-break by filename so identical mtimes (the names are timestamped) prune
    deterministically.
    """
    if keep <= 0:
        return []
    d = Path(dump_dir)
    dumps = [p for p in d.glob(f"*{_DUMP_SUFFIX}") if p.is_file()]
    dumps.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    deleted: list[str] = []
    for stale in dumps[keep:]:
        try:
            stale.unlink()
            deleted.append(str(stale))
        except OSError as exc:
            logger.warning("prune: could not delete %s: %s", stale, exc)
    return deleted


def _default_dump(main_dsn: str, out_path: str) -> None:
    subprocess.run(
        ["pg_dump", "--no-owner", "--no-privileges", "-Fc", "-f", out_path, main_dsn],
        check=True,
    )


def _default_restore(dump_path: str, spare_dsn: str) -> None:
    # --clean --if-exists so the restore overwrites the previous spare contents.
    subprocess.run(
        ["pg_restore", "--clean", "--if-exists", "--no-owner",
         "--no-privileges", "-d", spare_dsn, dump_path],
        check=True,
    )


def restore_dump(
    dump_path: str | os.PathLike,
    spare_dsn: str,
    *,
    restore_fn: RestoreFn = _default_restore,
) -> None:
    """Restore a single existing dump into the spare DB (the recovery console's
    one-click "Restore & run"). Raises on failure."""
    if not os.path.isfile(dump_path):
        raise FileNotFoundError(f"dump not found: {dump_path}")
    restore_fn(str(dump_path), spare_dsn)


def run_backup(
    *,
    main_dsn: str,
    spare_dsn: str,
    dump_dir: str | os.PathLike,
    keep: int,
    now: Optional[datetime] = None,
    marker_path: Optional[str] = None,
    dump_fn: DumpFn = _default_dump,
    restore_fn: RestoreFn = _default_restore,
) -> dict:
    """One backup tick: dump main -> file -> restore into spare -> manifest -> prune.

    Returns the manifest entry. ``restored_ok`` is ``False`` (the dump still
    kept as a valid backup) if the restore into the spare failed.

    Cron-clobber safety: when ``marker_path`` exists, mgmt is FAILED OVER and the
    spare is the LIVE database — the tick is skipped entirely (no dump, no
    restore) so a stale dump can never overwrite live writes on the spare. The
    returned dict carries ``{"skipped": "failover_active"}`` in that case.
    """
    if marker_path and os.path.exists(marker_path):
        logger.warning(
            "failover marker present (%s) — skipping backup tick to protect the live spare",
            marker_path,
        )
        return {"skipped": "failover_active", "marker": marker_path}
    now = now or datetime.now(timezone.utc)
    d = Path(dump_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out_path = str(d / f"mgmt_{stamp}{_DUMP_SUFFIX}")

    dump_fn(main_dsn, out_path)  # raises on failure -> the whole tick fails loudly

    restored_ok = True
    try:
        restore_fn(out_path, spare_dsn)
    except Exception as exc:  # noqa: BLE001 — the dump is still a valid backup
        restored_ok = False
        logger.error("restore into spare failed (dump kept as backup): %s", exc)

    entry = {
        "file": os.path.basename(out_path),
        "taken_at": now.isoformat(),
        "size": os.path.getsize(out_path),
        "sha256": sha256_file(out_path),
        "source": main_dsn.split("@")[-1] if "@" in main_dsn else "main",
        "restored_ok": restored_ok,
    }
    append_manifest(d / MANIFEST_NAME, entry, keep)
    prune_dumps(d, keep)
    return entry


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="mgmt DB main -> warm spare dump/restore")
    ap.add_argument("--main-dsn", default=os.environ.get("MAIN_DSN", ""))
    ap.add_argument("--spare-dsn", default=os.environ.get("SPARE_DSN", ""))
    ap.add_argument("--dump-dir", default=os.environ.get("DUMP_DIR", "/var/lib/sm-mgmt/backups"))
    ap.add_argument("--keep", type=int, default=int(os.environ.get("KEEP", "4")))
    ap.add_argument("--marker-path", default=os.environ.get("MARKER_PATH", ""))
    args = ap.parse_args(argv)
    if not args.main_dsn or not args.spare_dsn:
        ap.error("--main-dsn and --spare-dsn (or MAIN_DSN/SPARE_DSN env) are required")
    try:
        entry = run_backup(
            main_dsn=args.main_dsn, spare_dsn=args.spare_dsn,
            dump_dir=args.dump_dir, keep=args.keep,
            marker_path=args.marker_path or None,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("backup tick failed: %s", exc)
        return 1
    if entry.get("skipped"):
        logger.warning("backup tick skipped: %s", entry["skipped"])
        return 0
    logger.info("backup ok: %s (%d bytes, restored_ok=%s)",
                entry["file"], entry["size"], entry["restored_ok"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
