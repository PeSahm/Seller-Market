"""Stack provisioner orchestration (Phase 3).

This module is the seam between the HTTP router and the lower-level pieces
that materialise an agent's docker stack on a remote server:

* :mod:`app.models.stacks` for the ``agent_stacks`` row
* :mod:`app.services.rendering` for the five config files
  (``docker-compose.yml``, ``.env``, ``config.ini``, ``scheduler_config.json``,
  ``locust_config.json``)
* :mod:`app.services.ssh.commands` to run ``mkdir``, ``touch``,
  ``docker compose up/down`` on the remote
* :mod:`app.services.ssh.sftp` to atomically push the rendered files
* :mod:`app.models.audit` for write-side audit logging

Concurrency
-----------
``provision_stack``, ``redeploy_stack``, and ``deprovision_stack`` all
serialise on a Postgres advisory lock keyed on
``hash_lock_key("compose", server_id)``. Two admins clicking "provision" on
two different agents that happen to live on the same server can't race each
other into a half-applied compose graph. We use the *xact*-scoped variant
(``pg_try_advisory_xact_lock``) so the lock releases automatically when the
transaction commits or rolls back — no manual ``pg_advisory_unlock`` needed
and no risk of a leaked lock surviving a crashed worker.

If the lock can't be acquired immediately we raise ``RuntimeError`` rather
than block: a stuck compose command can take a long time and the admin
deserves an actionable error ("retry in a minute") instead of a spinner.

Defence in depth
----------------
Two guard layers prevent the mgmt UI from clobbering the existing root-level
``/root/seller-market/`` deployment:

1. :func:`app.services.ssh.sftp._assert_path_in_scope` — *per-server*; refuses
   any write outside ``server.base_dir``.
2. :func:`_assert_stack_path_in_scope` (here) — *per-stack*; refuses any
   operation whose path isn't strictly under ``stack.stack_dir``. This catches
   the case where ``stack_dir`` is somehow misaligned with ``base_dir`` (e.g.
   manual DB edit, future migration bug).

Phase 3 scope
-------------
Customers, scheduler jobs, and locust overrides are not yet wired into the
admin UI. We pass empty/None placeholders to the rendering layer for those
fields — Phase 4 (customers) and Phase 5 (scheduler / locust) will fill them
in. The renderers already accept the empty cases and produce sensible
defaults (no agent sections in config.ini, scheduler disabled, etc.).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import acquire_advisory_lock, hash_lock_key
from app.models.audit import AuditLog
from app.models.servers import Server
from app.models.settings import Setting
from app.models.stacks import AgentStack
from app.schemas.stack import StackActionResult
from app.services.rendering import (
    StackRenderContext,
    render_compose_yaml,
    render_config_ini,
    render_env,
    render_locust_config,
    render_scheduler_config,
)
from app.services.ssh.exceptions import PathOutOfScopeError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Defaults for global settings the rendering layer consumes. Parallel agent E
# owns the admin page that lets an operator override these via the
# ``settings`` table; we just read whatever is there and fall back if the
# row is missing (e.g. fresh DB before the operator has visited the page).
_DEFAULT_AGENT_IMAGE_TAG = "ghcr.io/pesahm/seller-market-scheduler:latest"
_DEFAULT_OCR_SERVICE_URL = "http://5.10.248.55:18080"

# Filenames the renderer produces, in the canonical order admins expect to
# see them in. Keeping this tuple here (rather than scattered across the
# action functions) means ``stack_files_preview`` and ``provision_stack``
# always agree on what files exist.
_STACK_FILES: tuple[str, ...] = (
    "docker-compose.yml",
    ".env",
    "config.ini",
    "scheduler_config.json",
    "locust_config.json",
)

# Files whose contents may legitimately contain passwords (the renderer
# already drops ``password_hash`` and Fernet tokens, but ``config.ini``
# carries broker passwords that ARE deliberately plaintext on the remote).
# We belt-and-braces redact obvious ``password = ...`` lines from these when
# rendering the preview for the UI.
_FILES_WITH_SECRETS: frozenset[str] = frozenset({".env", "config.ini"})

# Naive ``password = ...`` matcher. Captures any whitespace around the equals
# and the leading ``password`` token, replaces the value with ``<redacted>``.
# Matches both INI (``password = foo``) and dotenv (``PASSWORD=foo``) styles.
_PASSWORD_LINE_RE = re.compile(r"(?im)^(\s*password\s*=\s*).*$")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    """A timezone-aware UTC ``datetime`` for TIMESTAMPTZ columns."""
    return datetime.now(timezone.utc)


def _public_snapshot(stack: AgentStack) -> dict:
    """Audit-safe dict of an :class:`AgentStack` row.

    By design the row carries no secret material — only identifiers and
    operational state. We deliberately do NOT include rendered config
    contents in audit payloads even when they're easily reachable from the
    action functions; the audit log is intentionally narrow and operational.
    """
    return {
        "stack_id": str(stack.id),
        "server_id": str(stack.server_id),
        "agent_id": str(stack.agent_id),
        "compose_project": stack.compose_project,
        "status": stack.status,
    }


async def _write_audit(
    db: AsyncSession,
    *,
    actor_id: Optional[UUID],
    action: str,
    target_id: UUID,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
) -> None:
    """Insert a single ``audit_log`` row.

    ``target_type`` is always ``"stack"`` for this module. The caller is
    responsible for ensuring ``before`` / ``after`` contain no secret
    material — :func:`_public_snapshot` enforces this for the happy path.
    """
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action=action,
            target_type="stack",
            target_id=str(target_id),
            before_json=before,
            after_json=after,
        )
    )


def _assert_stack_path_in_scope(stack: AgentStack, remote_path: str) -> None:
    """Refuse any operation whose path isn't strictly under ``stack.stack_dir``.

    Mirrors the pattern in :func:`app.services.ssh.sftp._assert_path_in_scope`
    but narrows the scope from *server* to *stack*. This is the per-stack
    guard — defence-in-depth against a buggy ``stack_dir`` (e.g. ``""`` or
    ``"/"`` from a manual DB edit) leading to ``rm -rf`` of something wider
    than intended.

    Rules:

    * ``stack.stack_dir`` MUST be a non-root, non-empty absolute path.
    * After normalisation, ``remote_path`` MUST start with
      ``stack_dir + "/"`` — note the trailing slash; without it,
      ``"/foo/agents/abc"`` would match ``"/foo/agents/abc-evil/..."`` by
      simple prefix.
    * We normalise via the same :func:`posixpath.normpath` rules used in the
      SFTP layer so ``..`` traversal collapses before the check.

    Raises:
        PathOutOfScopeError: if ``remote_path`` is outside ``stack.stack_dir``
            or if ``stack_dir`` is itself unsafe (root / empty).
    """
    # posixpath kept local to the function to make the dependency narrow —
    # callers of stacks.py don't need to know about path normalisation rules.
    import posixpath  # noqa: WPS433

    stack_dir = stack.stack_dir or ""
    base = posixpath.normpath(stack_dir)
    if base in ("/", "", "."):
        raise PathOutOfScopeError(
            f"stack.stack_dir must be a non-root absolute path; got "
            f"{stack_dir!r}"
        )
    norm = posixpath.normpath(remote_path)
    if not norm.startswith(base + "/"):
        raise PathOutOfScopeError(
            f"refusing to operate outside stack scope: {remote_path!r} "
            f"(stack_dir={base!r})"
        )


async def _read_setting(
    db: AsyncSession, key: str, default: str
) -> str:
    """Read a value from the ``settings`` table, falling back to ``default``.

    The settings table is the operator-facing knob for things like the
    agent docker image tag and OCR service URL. If the row is missing (fresh
    DB, operator hasn't visited the settings page yet) we use a hard-coded
    default rather than blow up provisioning.
    """
    stmt = select(Setting.value).where(Setting.key == key)
    result = await db.execute(stmt)
    value = result.scalar_one_or_none()
    if value is None or value == "":
        return default
    return value


async def _build_render_context(
    db: AsyncSession,
    stack: AgentStack,
) -> StackRenderContext:
    """Assemble the rendering context for a stack from current DB state.

    Phase 3 wiring is intentionally narrow:

    * ``customers``, ``scheduler_jobs``, ``scheduler_enabled``, and
      ``locust`` are left at their dataclass defaults (empty / disabled).
      Phase 4 (customers) and Phase 5 (scheduler / locust) will wire them.
    * ``agent_image_tag`` and ``ocr_service_url`` are read from the
      ``settings`` table with hard-coded defaults.
    * ``server_base_dir`` comes from the server row that owns the stack.

    Raises:
        LookupError: if the stack's server row has gone missing — should be
            impossible because of the ``RESTRICT`` FK, but we surface a clean
            error rather than ``AttributeError`` if it ever happens.
    """
    server = await db.get(Server, stack.server_id)
    if server is None:
        raise LookupError(
            f"server {stack.server_id} for stack {stack.id} is missing"
        )

    image_tag = await _read_setting(
        db, "agent_image_tag", _DEFAULT_AGENT_IMAGE_TAG
    )
    ocr_url = await _read_setting(
        db, "ocr_service_url", _DEFAULT_OCR_SERVICE_URL
    )

    return StackRenderContext(
        agent_id=stack.agent_id,
        server_base_dir=server.base_dir,
        agent_image_tag=image_tag,
        ocr_service_url=ocr_url,
        # Phase 4 / 5 will populate these — empty defaults render cleanly.
        customers=(),
        scheduler_jobs=(),
        scheduler_enabled=False,
        locust=None,
    )


def _redact_secrets(filename: str, content: str) -> str:
    """Redact obvious ``password = ...`` lines from rendered files.

    The renderer is already careful — it never sees ``password_hash`` and
    Fernet tokens are decrypted by the caller — but ``config.ini``
    legitimately holds broker passwords in plaintext (that's the deployed
    format the trading bot expects). For the UI preview we hide those.
    """
    if filename not in _FILES_WITH_SECRETS:
        return content
    return _PASSWORD_LINE_RE.sub(r"\1<redacted>", content)


def _tail_log(stdout: str, stderr: str, *, max_lines: int = 20) -> str:
    """Combine stdout + stderr from a remote command and keep the last N lines.

    Used to build ``StackActionResult.log_tail``. The output already passes
    through ``run_command`` which decodes UTF-8 with replacement, so we don't
    have to worry about binary bytes here.
    """
    combined = (stdout or "") + "\n--- stderr ---\n" + (stderr or "")
    lines = combined.splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[-max_lines:])


def _import_ssh_commands():
    """Import ``run_command`` lazily so tests can avoid the SSH layer.

    Same pattern as :func:`app.services.servers._import_run_command`. The
    commands module is owned by a parallel agent; lazy import keeps our
    module-load free of any SSH dependency and lets the path-guard unit
    tests run without paramiko being importable.
    """
    from app.services.ssh.commands import run_command  # noqa: WPS433

    return run_command


def _import_sftp():
    """Import the SFTP helpers lazily — see :func:`_import_ssh_commands`."""
    from app.services.ssh.sftp import (  # noqa: WPS433
        sftp_atomic_write,
        sftp_read_text,
    )

    return sftp_atomic_write, sftp_read_text


def _shell_quote(s: str) -> str:
    r"""Single-quote a string for safe POSIX shell use.

    Mirrors :func:`app.services.ssh.sftp._shell_quote` (duplicated locally
    so we don't reach into another module's private helper). Wrap in
    ``'...'``, escape any internal ``'`` as ``'\''``.
    """
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_stacks(db: AsyncSession) -> list[AgentStack]:
    """Return all stacks ordered by ``deployed_at`` desc (newest first).

    Stacks that have never been provisioned have ``deployed_at IS NULL`` —
    we surface them at the top of the list because they're the ones an
    admin is most likely actively working on.
    """
    stmt = select(AgentStack).order_by(
        AgentStack.deployed_at.desc().nullsfirst()
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_stack(db: AsyncSession, stack_id: UUID) -> Optional[AgentStack]:
    """Look up a single stack by id."""
    stmt = select(AgentStack).where(AgentStack.id == stack_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def find_or_create_stack(
    db: AsyncSession,
    server: Server,
    agent_id: UUID,
    actor_id: UUID,
) -> AgentStack:
    """Idempotently materialise an ``agent_stacks`` row for ``(server, agent)``.

    The unique constraint ``uq_agent_stacks_server_agent`` enforces one
    stack per (server, agent) pair, so this is the safe entry point:

    * If a row already exists, return it untouched.
    * Otherwise compute the conventional ``stack_dir`` and ``compose_project``
      values, insert a row in ``status='provisioning'``, audit the create,
      commit, and refresh.

    ``stack_dir`` is laid out as ``<base_dir>/<agent_id>``. Using the
    agent's UUID as the leaf directory name keeps it filesystem-safe (only
    ``[0-9a-f-]``) and avoids any collision risk from agent usernames.

    ``compose_project`` is ``sm-agent-<agent_id>``. The ``sm-`` prefix lets
    a remote admin see at a glance which compose projects belong to the
    mgmt UI and which (if any) are leftovers from earlier deployments.
    """
    existing_stmt = select(AgentStack).where(
        AgentStack.server_id == server.id,
        AgentStack.agent_id == agent_id,
    )
    existing = await db.execute(existing_stmt)
    stack = existing.scalar_one_or_none()
    if stack is not None:
        return stack

    # base_dir is validated at the schema layer to be an absolute POSIX path
    # with no trailing slash and no '..' segments, but we belt-and-braces
    # strip any trailing slash here in case a future migration loosens that.
    base = server.base_dir.rstrip("/")
    stack = AgentStack(
        server_id=server.id,
        agent_id=agent_id,
        stack_dir=f"{base}/{agent_id}",
        compose_project=f"sm-agent-{agent_id}",
        status="provisioning",
        deployed_at=None,
    )
    db.add(stack)
    # Flush so the DB-side default (gen_random_uuid) populates stack.id
    # before we reference it in the audit row.
    await db.flush()

    await _write_audit(
        db,
        actor_id=actor_id,
        action="stack.create",
        target_id=stack.id,
        before=None,
        after=_public_snapshot(stack),
    )
    await db.commit()
    await db.refresh(stack)
    return stack


# ---------------------------------------------------------------------------
# Provision / redeploy / deprovision
# ---------------------------------------------------------------------------


async def _acquire_compose_lock(
    db: AsyncSession, server_id: UUID
) -> None:
    """Acquire the per-server compose advisory lock or raise.

    All three compose actions (provision, redeploy, deprovision) serialise on
    this lock so two admins can't race each other into a corrupted compose
    graph on the same server. The lock is xact-scoped so it releases
    automatically on commit/rollback — no manual cleanup, no risk of a leaked
    lock surviving a crash.

    We use ``pg_try_advisory_xact_lock`` (non-blocking) rather than
    ``pg_advisory_xact_lock`` so a stuck compose operation doesn't queue up
    a wall of waiters; instead, the second admin gets a clear
    "retry in a minute" error.
    """
    key = hash_lock_key("compose", str(server_id))
    acquired = await acquire_advisory_lock(db, key, transaction_scoped=True)
    if not acquired:
        raise RuntimeError(
            "another compose op is in flight for this server, retry"
        )


async def _render_all_files(
    db: AsyncSession, stack: AgentStack
) -> dict[str, str]:
    """Render every stack file into a ``{filename: content}`` dict.

    Pure function from the stack/DB perspective: it reads settings via
    :func:`_build_render_context` but performs no SSH and no writes. Kept
    separate from the SFTP push so we can grow a "preview before deploy"
    feature in a later phase without duplicating the wiring.
    """
    ctx = await _build_render_context(db, stack)
    return {
        "docker-compose.yml": render_compose_yaml(ctx),
        ".env": render_env(ctx),
        "config.ini": render_config_ini(ctx),
        "scheduler_config.json": render_scheduler_config(ctx),
        "locust_config.json": render_locust_config(ctx),
    }


async def _push_files(
    server: Server,
    stack: AgentStack,
    files: dict[str, str],
) -> None:
    """SFTP-push every file to ``<stack.stack_dir>/<filename>``.

    Each path is checked against both the server-level scope (via the SFTP
    helper itself) and the stack-level scope (via
    :func:`_assert_stack_path_in_scope`). Belt and braces — the renderer
    only emits filenames we control, but the cost of an extra string compare
    is trivial.
    """
    sftp_atomic_write, _ = _import_sftp()
    for filename, content in files.items():
        remote_path = f"{stack.stack_dir}/{filename}"
        _assert_stack_path_in_scope(stack, remote_path)
        await sftp_atomic_write(server, remote_path, content)


async def _compose_up(
    server: Server, stack: AgentStack
) -> tuple[bool, str]:
    """Run ``docker compose up -d`` for the stack. Returns (ok, log_tail).

    We don't pass ``check=True`` because we want to capture the failure
    output for the admin instead of raising an opaque exception. The caller
    inspects ``ok`` to decide whether to flip status to ``up`` or ``down``.

    The compose project name is shell-quoted defensively even though
    ``find_or_create_stack`` only ever produces ``sm-agent-<uuid>``-shaped
    values: UUIDs are ``[0-9a-f-]`` and present no quoting hazard, but a
    future change to the naming scheme shouldn't open a shell-injection
    hole. Same reasoning for ``stack_dir``.
    """
    run_command = _import_ssh_commands()
    cmd = (
        f"docker compose -p {_shell_quote(stack.compose_project)} "
        f"-f {_shell_quote(stack.stack_dir + '/docker-compose.yml')} up -d"
    )
    # 5-minute timeout: image pulls can be slow on a cold remote.
    result = await run_command(server, cmd, timeout=300.0, check=False)
    log_tail = _tail_log(result.stdout, result.stderr)
    return result.ok, log_tail


async def _compose_down(
    server: Server, stack: AgentStack
) -> tuple[bool, str]:
    """Run ``docker compose down --volumes --remove-orphans`` for the stack.

    ``--volumes`` removes the named volumes the stack defined — admins
    invoking "deprovision" expect a clean teardown, not a half-cleared box.
    ``--remove-orphans`` sweeps up any container in the compose project that
    isn't in the current ``docker-compose.yml`` (e.g. a service that was
    renamed mid-deploy).

    Like ``_compose_up``, we don't ``check=True`` so the caller can show the
    admin the actual stderr if it fails (e.g. "no such project" if the stack
    was never up — which is fine, the row can still be removed).
    """
    run_command = _import_ssh_commands()
    cmd = (
        f"docker compose -p {_shell_quote(stack.compose_project)} "
        f"down --volumes --remove-orphans"
    )
    result = await run_command(server, cmd, timeout=120.0, check=False)
    log_tail = _tail_log(result.stdout, result.stderr)
    return result.ok, log_tail


async def _prepare_remote_dirs(
    server: Server, stack: AgentStack
) -> None:
    """Create the stack subdirectories and touch the log files.

    The trading-bot container bind-mounts ``logs/``, ``order_results/``,
    ``.cache/``, and the two log files. Docker will silently create a
    *directory* at the bind-mount target if the source doesn't exist, which
    breaks the trading bot when it tries to ``open()`` the log file. We
    pre-touch the files so the bind mounts come up as files-not-dirs.

    ``check=True`` here because if mkdir/touch fails on the remote, every
    subsequent SFTP write will also fail with a less actionable error.
    """
    run_command = _import_ssh_commands()
    sd = _shell_quote(stack.stack_dir)

    # mkdir -p is idempotent — safe to run on re-provision too, but we
    # only do so from provision_stack itself; redeploy skips this.
    mkdir_cmd = (
        f"mkdir -p {sd}/logs {sd}/order_results {sd}/.cache"
    )
    await run_command(server, mkdir_cmd, timeout=30.0, check=True)

    # touch -a would update access time only; plain touch creates if absent
    # and bumps mtime if present. We want create-if-absent semantics so the
    # bind mount lands on a file.
    touch_cmd = (
        f"touch {sd}/trading_bot.log {sd}/cache_warmup.log"
    )
    await run_command(server, touch_cmd, timeout=30.0, check=True)


async def _do_compose_action(
    db: AsyncSession,
    stack_id: UUID,
    actor_id: Optional[UUID],
    *,
    audit_action: str,
    prepare_dirs: bool,
) -> StackActionResult:
    """Shared implementation for provision and redeploy.

    Both actions follow the same shape:

    1. Acquire the per-server advisory lock.
    2. Flip status to ``provisioning``.
    3. Render all five files.
    4. (Optional) mkdir + touch on the remote — only on first provision.
    5. SFTP-push the files.
    6. ``docker compose up -d``.
    7. Flip status to ``up`` (success) or ``down`` (failure) and audit.

    The only difference is the audit action name and whether we run the
    directory preparation step. Keeping the logic in one place means
    "redeploy" can never accidentally drift from "provision" in subtle ways.
    """
    stack = await get_stack(db, stack_id)
    if stack is None:
        raise LookupError(f"stack {stack_id} not found")

    # Hold the advisory lock for the duration of the action. Doing this
    # before any other DB work means we'd block immediately if a second
    # admin clicks "provision" — see _acquire_compose_lock for the
    # try-not-block rationale.
    await _acquire_compose_lock(db, stack.server_id)

    server = await db.get(Server, stack.server_id)
    if server is None:
        raise LookupError(
            f"server {stack.server_id} for stack {stack.id} is missing"
        )

    # Step 2: mark as in-progress so the UI can show a spinner.
    before = _public_snapshot(stack)
    stack.status = "provisioning"
    await db.commit()
    # Re-load the stack into the session so subsequent attribute access on
    # ``stack`` works (commit expires attributes by default — but our
    # session has expire_on_commit=False, so this is a no-op safety net).
    await db.refresh(stack)

    log_tail = ""
    try:
        files = await _render_all_files(db, stack)
        if prepare_dirs:
            await _prepare_remote_dirs(server, stack)
        await _push_files(server, stack, files)
        ok, log_tail = await _compose_up(server, stack)
    except Exception as exc:
        # Any failure before/during compose flips status to ``down`` so the
        # next admin action knows we left the remote in an indeterminate
        # state. We commit the status update before re-raising so the
        # router can render the failure with a fresh DB row.
        stack.status = "down"
        await _write_audit(
            db,
            actor_id=actor_id,
            action="stack.error",
            target_id=stack.id,
            before=before,
            after=_public_snapshot(stack),
        )
        await db.commit()
        logger.exception(
            "compose action failed for stack %s: %s", stack.id, exc
        )
        raise

    # Step 7: finalise status based on compose exit code.
    if ok:
        stack.status = "up"
        stack.deployed_at = _now_utc()
        message = "stack deployed"
    else:
        stack.status = "down"
        message = "compose returned non-zero exit"

    await _write_audit(
        db,
        actor_id=actor_id,
        action=audit_action,
        target_id=stack.id,
        before=before,
        after=_public_snapshot(stack),
    )
    await db.commit()
    await db.refresh(stack)

    return StackActionResult(
        ok=ok,
        stack_id=stack.id,
        status=stack.status,
        message=message,
        log_tail=log_tail,
    )


async def provision_stack(
    db: AsyncSession,
    stack_id: UUID,
    actor_id: Optional[UUID],
) -> StackActionResult:
    """First-time bring-up of an agent's docker stack.

    Includes the one-time directory preparation step (``mkdir``, ``touch``).
    Subsequent re-deploys should use :func:`redeploy_stack` which skips that
    work — it's idempotent on the remote either way, but skipping is faster
    and keeps the SSH log cleaner.
    """
    return await _do_compose_action(
        db,
        stack_id,
        actor_id,
        audit_action="stack.provision",
        prepare_dirs=True,
    )


async def redeploy_stack(
    db: AsyncSession,
    stack_id: UUID,
    actor_id: Optional[UUID],
) -> StackActionResult:
    """Re-render config + ``docker compose up -d`` for an existing stack.

    Useful when an admin changes the OCR URL or agent image tag in the
    settings table and wants those changes to take effect without rebuilding
    the directory tree. The mkdir/touch step is skipped because it's already
    done.
    """
    return await _do_compose_action(
        db,
        stack_id,
        actor_id,
        audit_action="stack.redeploy",
        prepare_dirs=False,
    )


async def deprovision_stack(
    db: AsyncSession,
    stack_id: UUID,
    actor_id: Optional[UUID],
) -> StackActionResult:
    """Tear down the stack and remove its row.

    Sequence:

    1. Acquire the per-server advisory lock.
    2. Flip status to ``deprovisioning``.
    3. ``docker compose down --volumes --remove-orphans``.
    4. ``rm -rf <stack_dir>`` — but ONLY if the path passes two safety
       checks (see below). This is the most dangerous remote command in the
       module; the checks are belt-and-braces.
    5. Delete the ``agent_stacks`` row.
    6. Audit ``stack.deprovision``.

    ``rm -rf`` safety:

    * ``stack.stack_dir`` MUST start with ``<server.base_dir>/<agent_id>``
      (so a stack row with a mismatched ``stack_dir`` from a buggy migration
      can't be used to nuke unrelated content).
    * ``server.base_dir`` MUST NOT be ``/`` (defence against a server row
      that was somehow created with a root base_dir — the schema-level
      validator should already prevent this, but we re-check at the point
      of use).

    If either check fails we abort BEFORE running rm: the stack row stays in
    place and the admin can investigate. We surface the failure as a
    :class:`PathOutOfScopeError`.
    """
    stack = await get_stack(db, stack_id)
    if stack is None:
        raise LookupError(f"stack {stack_id} not found")

    await _acquire_compose_lock(db, stack.server_id)

    server = await db.get(Server, stack.server_id)
    if server is None:
        raise LookupError(
            f"server {stack.server_id} for stack {stack.id} is missing"
        )

    before = _public_snapshot(stack)
    stack.status = "deprovisioning"
    await db.commit()
    await db.refresh(stack)

    # Step 3: compose down. We don't bail on a non-zero exit — "no such
    # project" is a valid result if the stack was never up, and we still
    # want to clean up the directory and the row.
    log_tail = ""
    try:
        _, log_tail = await _compose_down(server, stack)
    except Exception as exc:
        # If SSH itself fails (auth error, host unreachable) we can't
        # safely remove the dir or the row — log + re-raise so the admin
        # can retry once the server is reachable. Status stays
        # ``deprovisioning`` so the UI can show "in progress" until then.
        await _write_audit(
            db,
            actor_id=actor_id,
            action="stack.error",
            target_id=stack.id,
            before=before,
            after=_public_snapshot(stack),
        )
        await db.commit()
        logger.exception(
            "compose down failed for stack %s: %s", stack.id, exc
        )
        raise

    # Step 4: rm -rf, gated by two safety checks.
    base = (server.base_dir or "").rstrip("/")
    expected_prefix = f"{base}/{stack.agent_id}"
    if not base or base == "/":
        raise PathOutOfScopeError(
            f"refusing to rm-rf: server.base_dir is unsafe ({base!r})"
        )
    if not stack.stack_dir.startswith(expected_prefix):
        raise PathOutOfScopeError(
            f"refusing to rm-rf: stack_dir {stack.stack_dir!r} does not "
            f"start with expected prefix {expected_prefix!r}"
        )

    run_command = _import_ssh_commands()
    rm_cmd = f"rm -rf {_shell_quote(stack.stack_dir)}"
    # check=False so we capture stderr but don't raise — we want to delete
    # the DB row even if the directory was already gone (idempotent
    # teardown). The log_tail surfaces any unexpected errors.
    rm_result = await run_command(server, rm_cmd, timeout=60.0, check=False)
    rm_log = _tail_log(rm_result.stdout, rm_result.stderr)
    if rm_log:
        log_tail = (log_tail + "\n--- rm ---\n" + rm_log) if log_tail else rm_log

    # Step 5 + 6: delete the row and audit. Audit BEFORE delete so the
    # target_id reference still makes sense to a future reader.
    await _write_audit(
        db,
        actor_id=actor_id,
        action="stack.deprovision",
        target_id=stack.id,
        before=before,
        after=None,
    )
    await db.delete(stack)
    await db.commit()

    return StackActionResult(
        ok=True,
        stack_id=stack_id,
        status="deprovisioned",
        message="stack torn down",
        log_tail=log_tail,
    )


# ---------------------------------------------------------------------------
# Preview (read-only)
# ---------------------------------------------------------------------------


async def stack_files_preview(
    db: AsyncSession,
    stack_id: UUID,
) -> dict[str, str]:
    """SFTP-read every stack file and return ``{filename: content}``.

    Used by the stack detail page to show the admin what's actually on the
    remote (which may differ from what we'd render today if the settings
    table has changed since the last deploy). Each file is read via
    :func:`app.services.ssh.sftp.sftp_read_text` — that helper has no
    scope guard, but we apply our own per-stack guard here before issuing
    the read so an admin viewing this page can never trick the mgmt UI into
    reading ``/etc/shadow`` via a buggy ``stack_dir``.

    Missing files (e.g. the stack was never provisioned, or someone deleted
    a file by hand) return the literal string ``"<not yet rendered>"`` for
    that key — sensible UX, no need to special-case in the template.

    Secret redaction:
        For ``.env`` and ``config.ini`` we redact ``password = ...`` lines
        before returning. The renderer is already careful — it never sees
        ``password_hash`` and Fernet tokens are decrypted by the caller —
        but ``config.ini`` legitimately holds broker passwords in plaintext
        (that's the format the deployed trading bot consumes). For the UI
        preview we don't want them on screen.
    """
    stack = await get_stack(db, stack_id)
    if stack is None:
        raise LookupError(f"stack {stack_id} not found")

    server = await db.get(Server, stack.server_id)
    if server is None:
        raise LookupError(
            f"server {stack.server_id} for stack {stack.id} is missing"
        )

    _, sftp_read_text = _import_sftp()

    out: dict[str, str] = {}
    for filename in _STACK_FILES:
        remote_path = f"{stack.stack_dir}/{filename}"
        # Per-stack scope guard — defence in depth against a misaligned
        # stack_dir leading to a read outside the agent's directory.
        _assert_stack_path_in_scope(stack, remote_path)
        try:
            content = await sftp_read_text(server, remote_path)
        except Exception as exc:  # noqa: BLE001 — file might just not exist
            logger.debug(
                "stack file %s missing or unreadable: %s", remote_path, exc
            )
            out[filename] = "<not yet rendered>"
            continue
        out[filename] = _redact_secrets(filename, content)
    return out


__all__ = [
    "deprovision_stack",
    "find_or_create_stack",
    "get_stack",
    "list_stacks",
    "provision_stack",
    "redeploy_stack",
    "stack_files_preview",
]
