"""DB-independent recovery console (#156).

Mounted as the ONLY router when ``MGMT_RECOVERY_MODE=true``. The whole point is
that this works **when the main database is down** — so it touches NO database,
NO ORM, NO ``app.db`` session. It:

* authenticates with a single shared ``MGMT_RECOVERY_TOKEN`` (constant-time
  compare, fail-closed if unset) — the normal DB-backed login is unavailable;
* lists backups from the on-disk JSON manifest (``app.services.db_backup``);
* shows whether the main DB is reachable (a best-effort TCP probe);
* **one-click "Restore & run"**: ``pg_restore`` a chosen dump into the LOCAL
  spare, then run an optional post-restore command to bring mgmt up on the
  spare.

SECURITY: this is powerful (it can restore the DB + run a shell command), so it
MUST be reachable only over WireGuard / loopback, never publicly exposed. The
token is the gate; keep ``MGMT_RECOVERY_TOKEN`` secret.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
from html import escape
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse

from app.services import db_backup
from app.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recovery", tags=["recovery"])


def _token_ok(provided: Optional[str]) -> bool:
    """Constant-time token check; fail closed when the token is unset."""
    import secrets

    expected = get_settings().recovery_token.get_secret_value()
    if not expected:
        return False
    return bool(provided) and secrets.compare_digest(provided, expected)


def _main_db_status() -> str:
    """Best-effort: is the configured main DB's host:port reachable? (TCP probe.)

    Returns ``"reachable"`` / ``"unreachable"`` / ``"unknown"``. This is only a
    hint for the operator — a port being open doesn't prove the DB is healthy.
    """
    try:
        url = urlparse(get_settings().database_url.replace("+asyncpg", ""))
        host, port = url.hostname, url.port or 5432
        if not host:
            return "unknown"
        with socket.create_connection((host, port), timeout=3):
            return "reachable"
    except OSError:
        return "unreachable"
    except Exception:  # noqa: BLE001 — never let the console crash on this
        return "unknown"


def _manifest_path() -> Path:
    return Path(get_settings().backup_dir) / db_backup.MANIFEST_NAME


def _page(body: str, *, status_code: int = 200) -> HTMLResponse:
    css = '<link rel="stylesheet" href="/static/css/app.css">'
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Recovery — Seller-Market</title>{css}"
        "<style>body{max-width:880px;margin:2rem auto;padding:0 1rem;font-family:system-ui,sans-serif}"
        ".rec-banner{background:#7a1f1f;color:#fff;padding:.6rem 1rem;border-radius:6px;margin-bottom:1rem}"
        "table{width:100%;border-collapse:collapse}td,th{padding:.4rem .5rem;border-bottom:1px solid #ddd;text-align:left}"
        "</style></head><body>"
        "<div class='rec-banner'><strong>RECOVERY MODE</strong> — the main database is unavailable. "
        "This console is DB-independent.</div>"
        f"{body}</body></html>"
    )
    return HTMLResponse(html, status_code=status_code)


def _token_form(msg: str = "") -> HTMLResponse:
    warn = f"<p style='color:#a00'>{escape(msg)}</p>" if msg else ""
    body = (
        f"{warn}"
        "<h2>Enter recovery token</h2>"
        "<form method='post' action='/recovery'>"
        "<input type='password' name='token' placeholder='MGMT_RECOVERY_TOKEN' "
        "style='padding:.5rem;width:60%' autofocus>"
        "<button type='submit' style='padding:.5rem 1rem'>Unlock</button>"
        "</form>"
    )
    return _page(body, status_code=401)


def _console(token: str, *, flash: str = "") -> HTMLResponse:
    entries = db_backup.load_manifest(_manifest_path())
    db = _main_db_status()
    flash_html = (
        f"<div style='background:#eef;padding:.5rem 1rem;border-radius:6px;margin:.5rem 0'>{escape(flash)}</div>"
        if flash else ""
    )
    rows = []
    for e in reversed(entries):  # newest first
        fname = escape(str(e.get("file", "")))
        taken = escape(str(e.get("taken_at", "")))
        size = e.get("size", 0)
        try:
            size_mb = f"{int(size) / (1024 * 1024):.1f} MB"
        except (TypeError, ValueError):
            size_mb = "?"
        restored = "✓" if e.get("restored_ok") else "—"
        rows.append(
            f"<tr><td><code>{fname}</code></td><td>{taken}</td><td>{size_mb}</td>"
            f"<td>{restored}</td><td>"
            "<form method='post' action='/recovery/restore' "
            "onsubmit=\"return confirm('Restore this dump into the spare and bring mgmt up?')\">"
            f"<input type='hidden' name='token' value='{escape(token)}'>"
            f"<input type='hidden' name='file' value='{fname}'>"
            "<button type='submit'>Restore &amp; run</button></form></td></tr>"
        )
    table = (
        "<table><tr><th>Backup</th><th>Taken (UTC)</th><th>Size</th>"
        "<th>spare ok</th><th></th></tr>"
        + ("".join(rows) if rows else "<tr><td colspan=5><em>no backups in manifest</em></td></tr>")
        + "</table>"
    )
    badge = {"reachable": "#1a7f37", "unreachable": "#a00"}.get(db, "#777")
    body = (
        f"{flash_html}"
        f"<p>Main DB: <strong style='color:{badge}'>{db}</strong> "
        f"&middot; backups dir: <code>{escape(get_settings().backup_dir)}</code></p>"
        "<h2>Backups</h2>"
        f"{table}"
        "<p style='color:#777;margin-top:1rem'>“Restore &amp; run” restores the chosen dump into "
        "the local spare, then runs the configured post-restore command to bring mgmt up on the spare.</p>"
    )
    return _page(body)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def recovery_home(token: Optional[str] = None) -> HTMLResponse:
    """Show the console if a valid token is supplied (?token=...), else the form."""
    if _token_ok(token):
        return _console(token or "")
    return _token_form()


@router.post("", response_class=HTMLResponse)
@router.post("/", response_class=HTMLResponse)
async def recovery_login(token: str = Form("")) -> HTMLResponse:
    if _token_ok(token):
        return _console(token)
    return _token_form("Invalid recovery token." if token else "")


@router.post("/restore", response_class=HTMLResponse)
async def recovery_restore(token: str = Form(""), file: str = Form("")) -> HTMLResponse:
    if not _token_ok(token):
        return _token_form("Invalid recovery token.")
    settings = get_settings()
    # Path-traversal guard: only a bare filename inside backup_dir.
    if not file or os.path.basename(file) != file:
        return _console(token, flash="Invalid backup file name.")
    dump_path = Path(settings.backup_dir) / file
    if not dump_path.is_file():
        return _console(token, flash=f"Backup not found: {file}")
    if not settings.spare_dsn:
        return _console(token, flash="SPARE_DSN is not configured — cannot restore.")

    try:
        await asyncio.to_thread(db_backup.restore_dump, dump_path, settings.spare_dsn)
    except Exception as exc:  # noqa: BLE001 — surface the failure to the operator
        logger.error("recovery restore failed: %s", exc)
        return _console(token, flash=f"Restore FAILED: {exc}")

    msg = f"Restored {file} into the spare."
    cmd = settings.recovery_post_restore_cmd
    if cmd:
        try:
            out = await asyncio.to_thread(
                lambda: subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
            )
            msg += f" Post-restore (rc={out.returncode}): {(out.stdout or out.stderr or '').strip()[:300]}"
        except Exception as exc:  # noqa: BLE001
            msg += f" Post-restore command FAILED: {exc}"
    else:
        msg += " Now bring mgmt up against the spare (no post-restore command configured)."
    return _console(token, flash=msg)
