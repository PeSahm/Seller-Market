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
other into a half-applied compose graph.

We use the **session-scoped** variant (``pg_try_advisory_lock`` /
``pg_advisory_unlock``) on a *dedicated* AsyncSession opened just for the
lock. The transaction-scoped variant would release the lock on the very
first ``commit()`` we issue (to flip status to ``provisioning``), leaving
all the subsequent SFTP + ``docker compose up`` work unprotected. By
holding the lock on a side-channel session, the regular ``db`` session can
commit freely while the lock stays held until the ``finally`` block
explicitly releases it after the remote work completes (or fails).

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

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import (
    AsyncSessionLocal,
    hash_lock_key,
    release_session_lock,
    try_acquire_session_lock,
)
from app.models.audit import AuditLog
from app.models.customers import Customer
from app.models.servers import Server
from app.models.settings import Setting
from app.models.stacks import AgentStack
from app.schemas.stack import StackActionResult
from app.services.rendering import (
    CustomerRow,
    LocustConfigRow,
    SchedulerJobRow,
    StackRenderContext,
    render_compose_yaml,
    render_config_ini,
    render_env,
    render_locust_config,
    render_scheduler_config,
)
from app.services.ssh.exceptions import PathOutOfScopeError, SSHError

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


async def _demote_stack_customers(
    db: AsyncSession, stack_id: UUID, actor_id: Optional[UUID]
) -> int:
    """Demote a deprovisioned stack's customers to 'pending' so they stay
    VISIBLE in the admin inbox for re-placement.

    Without this, deleting the stack row leaves its customers
    ``assignment_status='active'`` with ``stack_id`` NULL (the FK is
    ``ON DELETE SET NULL``) — invisible to BOTH the config renderer (which
    selects ``WHERE stack_id == stack.id``) and the Pending inbox
    (``status='pending'``), so they silently stop trading. Clearing
    server_id/stack_id and setting 'pending' lands them back in the inbox.

    Returns the number demoted. Does NOT commit — runs inside the
    deprovision transaction so the demote + the row delete commit together.
    """
    ids = list(
        (
            await db.execute(
                select(Customer.id).where(Customer.stack_id == stack_id)
            )
        )
        .scalars()
        .all()
    )
    if not ids:
        return 0
    await db.execute(
        update(Customer)
        .where(Customer.id.in_(ids))
        .values(
            assignment_status="pending",
            server_id=None,
            stack_id=None,
            updated_at=_now_utc(),
        )
    )
    await _write_audit(
        db,
        actor_id=actor_id,
        action="stack.demote_customers",
        target_id=stack_id,
        before={"count": len(ids)},
        after={"status": "pending"},
    )
    return len(ids)


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


def _assert_rm_target_in_scope(
    stack_dir: str, base_dir: str, agent_id: UUID | str
) -> None:
    """Refuse a ``rm -rf`` whose target isn't the stack's own directory.

    Pure-function half of the deprovision-time guard so it can be exercised
    directly in unit tests without standing up a full ``deprovision_stack``
    invocation. The deprovision flow calls this immediately before issuing
    the remote ``rm -rf``.

    Rules:

    * ``base_dir`` MUST be a non-root, non-empty absolute path (trailing
      slash is normalised away first).
    * ``stack_dir`` MUST equal ``<base_dir>/<agent_id>`` exactly OR start
      with that prefix followed by a literal ``"/"``. The trailing-slash
      requirement is the load-bearing piece: a bare
      ``stack_dir.startswith(expected_prefix)`` would happily accept
      sibling directories like ``<base>/<uuid>-evil``.

    Raises:
        PathOutOfScopeError: if ``base_dir`` is unsafe or ``stack_dir`` is
            outside the expected per-agent prefix.
    """
    base = (base_dir or "").rstrip("/")
    if not base or base == "/":
        raise PathOutOfScopeError(
            f"refusing to rm-rf: server.base_dir is unsafe ({base_dir!r})"
        )
    expected_prefix = f"{base}/{agent_id}"
    if not (
        stack_dir == expected_prefix
        or stack_dir.startswith(expected_prefix + "/")
    ):
        raise PathOutOfScopeError(
            f"refusing to rm-rf: stack_dir {stack_dir!r} is not within "
            f"expected prefix {expected_prefix!r}"
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


def _parse_bool(value: str) -> bool:
    """Parse a settings-table string into a bool (``"true"/"1"/"yes"/"on"``)."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


async def _build_render_context(
    db: AsyncSession,
    stack: AgentStack,
) -> StackRenderContext:
    """Build the per-stack render context.

    Phase 5: everything is wired up. Customers come from the customers
    service (with broker passwords decrypted at the source so the rendering
    layer never touches Fernet), scheduler job rows come from the
    scheduler_jobs service, and the locust override (if any) comes from the
    locust_configs service.

    * ``agent_image_tag`` and ``ocr_service_url`` are read from the
      ``settings`` table with hard-coded defaults.
    * ``server_base_dir`` comes from the server row that owns the stack.
    * ``customers`` are loaded by :func:`_load_stack_customers`.
    * ``scheduler_jobs`` are loaded from the scheduler_jobs service and
      projected into :class:`SchedulerJobRow`. The DB stores ``time`` as
      ``sa.Time``; the renderer wants the ``"HH:MM:SS"`` string form, so we
      coerce here at the seam.
    * ``scheduler_enabled`` is **always True**. The top-level ``"enabled"``
      flag in the bot's scheduler is NOT a kill-switch: it controls the
      poll cadence — ``True`` polls every 1 second, ``False`` sleeps 60
      seconds between checks
      ([SellerMarket/scheduler.py:252](SellerMarket/scheduler.py#L252)).
      The real on/off toggle is each job's own ``enabled`` field. We keep
      the top-level at ``True`` so a user enabling a previously-disabled
      job sees it fire within ~1 second instead of waiting up to 60 s for
      the slow loop to wake up — matches the convention of the original
      ``SellerMarket/scheduler_config.json``.
    * ``locust`` is the per-agent override row (or None to fall back to
      fleet defaults inside the renderer).

    Service imports are lazy to match the existing customer-service pattern
    — keeps module-load free of any indirect cycle through services that
    may grow a dependency back into this module.

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
    # #110 auto-sell: empty by default → bot keeps the scheduler-only command.
    bot_market_data_url = (await _read_setting(db, "bot_market_data_url", "") or "").strip()

    customers = await _load_stack_customers(db, stack.id)

    # Phase 5: real scheduler jobs + locust config. Lazy imports mirror the
    # services_customers pattern in _load_stack_customers — keeps the
    # module import graph acyclic regardless of what those services pull in.
    from app.services import (  # noqa: WPS433
        locust_configs as services_locust,
    )
    from app.services import (  # noqa: WPS433
        scheduler_jobs as services_scheduler,
    )

    scheduler_db_rows = await services_scheduler.list_jobs(
        db, stack_id=stack.id
    )
    scheduler_rows = tuple(
        SchedulerJobRow(
            name=j.name,
            # DB stores sa.Time → renderer wants "HH:MM:SS" string.
            time=j.time.strftime("%H:%M:%S"),
            enabled=j.enabled,
            command=j.command,
        )
        for j in scheduler_db_rows
    )
    # Top-level "enabled" is ALWAYS True. It controls the bot's poll cadence
    # (1s when True, 60s when False — see SellerMarket/scheduler.py:252),
    # not whether any job runs. The real on/off toggle is each job's own
    # `enabled` field, which `should_run_job` checks separately. Keeping the
    # top-level True means a user enabling a job sees it fire within 1s
    # instead of waiting up to 60s for the slow loop to notice.
    scheduler_enabled = True

    locust_db = await services_locust.get_locust_config(db, stack.id)
    locust_row: LocustConfigRow | None = None
    if locust_db is not None:
        locust_row = LocustConfigRow(
            users=locust_db.users,
            spawn_rate=locust_db.spawn_rate,
            run_time=locust_db.run_time,
            host=locust_db.host,
            processes=locust_db.processes,
        )

    # Locust auto-scale: by default the renderer derives users/spawn from the
    # customer-section count (so locust never spawns fewer users than there are
    # customers). The operator can disable it; the "3×" multiplier is tunable.
    autoscale_locust = _parse_bool(
        await _read_setting(db, "enable_locust_autoscale", "true")
    )
    try:
        users_multiplier = int(
            await _read_setting(db, "autobalance_users_multiplier", "3")
        )
    except (TypeError, ValueError):
        users_multiplier = 3
    users_multiplier = max(1, users_multiplier)

    # DB-pushed bot [runtime] overrides (broker/market-data hosts, exir fee, time
    # windows, OCR pool, ...). Projected from the full settings dict so a single
    # edit + fleet push redirects them with no image rebuild.
    from app.services import settings_store as services_settings  # noqa: WPS433
    runtime_overrides = services_settings.build_runtime_section(
        await services_settings.get_all_settings(db)
    )

    return StackRenderContext(
        agent_id=stack.agent_id,
        server_base_dir=server.base_dir,
        agent_image_tag=image_tag,
        ocr_service_url=ocr_url,
        customers=customers,
        scheduler_jobs=scheduler_rows,
        scheduler_enabled=scheduler_enabled,
        locust=locust_row,
        autoscale_locust=autoscale_locust,
        locust_users_multiplier=users_multiplier,
        bot_market_data_url=bot_market_data_url,
        runtime=runtime_overrides,
    )


async def _load_stack_customers(
    db: AsyncSession, stack_id: UUID
) -> tuple[CustomerRow, ...]:
    """Load Customer × TradeInstruction rows for ``stack_id``.

    Post-migration 0003, a Customer's per-trade fields (isin, side,
    section_name) live on a separate ``trade_instructions`` table. The
    renderer's contract is still "one section per tradeable position",
    so we cross-join customers with their trade instructions. Each
    ``CustomerRow`` carries the credentials pulled from Customer plus
    the (isin, side, section_name) from the TradeInstruction.

    Migration 0004 dropped the ``enabled`` columns from both tables —
    deletion is hard now, so anything still in the DB is meant to be
    rendered. A Customer with zero TradeInstructions contributes zero
    sections (delete its last TI to stop trading the account).

    Password decrypt goes through
    :func:`app.services.customers.decrypt_password` so the
    ``secret_decrypt`` audit-log entry fires once per customer (NOT
    once per trade). We decrypt before iterating the trades so the
    plaintext is reused across all that customer's instructions.

    The import is lazy because the customer service may grow a
    dependency on the distribution service that closes a cycle back
    into this module.
    """
    # Lazy import — keeps module-load free of any indirect cycle through the
    # distribution service (Phase 4 owners noted customers may import stacks
    # transitively).
    from app.services import customers as services_customers  # noqa: WPS433
    from app.models.trade_instructions import TradeInstruction  # noqa: WPS433

    stmt = select(Customer).where(Customer.stack_id == stack_id)
    result = await db.execute(stmt)
    customer_rows = list(result.scalars().all())

    rendered: list[CustomerRow] = []
    for c in customer_rows:
        ti_stmt = select(TradeInstruction).where(
            TradeInstruction.customer_id == c.id
        )
        ti_rows = list((await db.execute(ti_stmt)).scalars().all())
        if not ti_rows:
            continue

        # Decrypt once per customer; reuse the plaintext across all its
        # trade instructions. One audit-log entry per customer per
        # render — matches the pre-migration cost.
        password_plain = await services_customers.decrypt_password(c)
        for ti in ti_rows:
            rendered.append(
                CustomerRow(
                    section_name=ti.section_name,
                    username=c.username,
                    password_plain=password_plain,
                    broker=c.broker,
                    isin=ti.isin,
                    side=ti.side,
                    # getattr keeps older test fakes (built before this column)
                    # working; real ORM rows always carry the attribute.
                    auto_sell_threshold=getattr(ti, "auto_sell_threshold", None),
                    auto_sell_only=getattr(ti, "auto_sell_only", False),
                )
            )
    return tuple(rendered)


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

    # Seed the two default scheduler jobs (cache_warmup 08:30:00 / run_trading
    # 08:44:20) so a freshly-created stack is schedulable out of the box. Only
    # runs on the CREATE path (the early-return above skips existing stacks).
    # Best-effort: a seeding failure must not abort stack creation — the jobs
    # can be added/backfilled later.
    try:
        from app.services import scheduler_jobs as _scheduler_jobs  # noqa: WPS433
        await _scheduler_jobs.ensure_default_scheduler_jobs(db, stack.id, actor_id)
    except Exception:  # noqa: BLE001
        logger.exception("failed to seed default scheduler jobs for stack %s", stack.id)

    return stack


# ---------------------------------------------------------------------------
# Provision / redeploy / deprovision
# ---------------------------------------------------------------------------


def _compose_lock_key(server_id: UUID) -> int:
    """Stable advisory-lock key for the per-server compose serialisation.

    All three compose actions (provision, redeploy, deprovision) acquire
    this key on a dedicated session so they can't race each other into a
    corrupted compose graph on the same server. See the module docstring
    for the rationale on session-scoped (vs. transaction-scoped) locks.
    """
    return hash_lock_key("compose", str(server_id))


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
    server: Server,
    stack: AgentStack,
    *,
    force_recreate: bool = False,
) -> tuple[bool, str]:
    """Run ``docker compose up -d`` for the stack. Returns (ok, log_tail).

    We don't pass ``check=True`` because we want to capture the failure
    output for the admin instead of raising an opaque exception. The caller
    inspects ``ok`` to decide whether to flip status to ``up`` or ``down``.

    ``force_recreate=True`` adds ``--force-recreate --pull always`` so the
    container is destroyed and re-created from scratch. Used by
    :func:`redeploy_stack` because:

    * Each per-stack file is mounted as a **single-file bind mount**. Docker
      staples a single-file bind to the destination INODE at the moment of
      ``docker run`` — if the host file is replaced (e.g. anything that
      called the old ``mv -f``-based atomic write, or a manual edit via an
      editor that does atomic-rename-on-save), the container reads the
      original frozen inode forever. ``--force-recreate`` re-attaches the
      bind to the current inode.
    * Settings changes (e.g. ``agent_image_tag`` on /admin/settings) only
      take effect on container creation. ``--pull always`` ensures the new
      image is fetched even when the tag string is unchanged (e.g. a
      ``:latest`` push).
    * Generally useful as a "reset this stack" knob for the admin — if a
      container is in a weird state, redeploy now reliably fixes it.

    The ``--pull`` flag is derived from ``server.image_pull_policy``
    (added in #71-incremental). ``always`` reproduces the historical
    behaviour; ``never`` is the escape hatch for hosts where the registry
    is unreachable (Iranian VPSes where ghcr.io is blocked) — the operator
    pre-pulls + retags from a mirror manually and the redeploy then uses
    the local image without contacting the registry. ``missing`` is the
    docker-compose default (pull only if the image isn't local).

    The compose project name is shell-quoted defensively even though
    ``find_or_create_stack`` only ever produces ``sm-agent-<uuid>``-shaped
    values: UUIDs are ``[0-9a-f-]`` and present no quoting hazard, but a
    future change to the naming scheme shouldn't open a shell-injection
    hole. Same reasoning for ``stack_dir``.
    """
    run_command = _import_ssh_commands()
    # Map the per-server policy to docker-compose's --pull flag. We pass
    # an explicit value even for ``always`` so the behaviour doesn't drift
    # if compose's default ever changes.
    pull_policy = getattr(server, "image_pull_policy", "always") or "always"
    if pull_policy not in ("always", "missing", "never"):
        # Defensive: a bad DB value (which shouldn't be possible — the
        # column is an enum) shouldn't crash a redeploy. Fall back to
        # the historical default.
        logger.warning(
            "server %s has unknown image_pull_policy=%r — defaulting to 'always'",
            server.id, pull_policy,
        )
        pull_policy = "always"
    pull_flag = f" --pull {pull_policy}"
    flags = " up -d" + pull_flag
    if force_recreate:
        flags = " up -d --force-recreate" + pull_flag
    cmd = (
        f"docker compose -p {_shell_quote(stack.compose_project)} "
        f"-f {_shell_quote(stack.stack_dir + '/docker-compose.yml')}{flags}"
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

    We pass ``-f <stack_dir>/docker-compose.yml`` explicitly. Without it,
    ``docker compose down`` resolves the compose file relative to the SSH
    session's CWD (typically the login shell's ``$HOME``), which won't
    contain the file — so the command would silently target nothing and
    deprovision would falsely report success.

    Like ``_compose_up``, we don't ``check=True`` so the caller can show the
    admin the actual stderr if it fails (e.g. "no such project" if the stack
    was never up — which is fine, the row can still be removed).
    """
    run_command = _import_ssh_commands()
    cmd = (
        f"docker compose -p {_shell_quote(stack.compose_project)} "
        f"-f {_shell_quote(stack.stack_dir + '/docker-compose.yml')} "
        f"down --volumes --remove-orphans"
    )
    result = await run_command(server, cmd, timeout=120.0, check=False)
    log_tail = _tail_log(result.stdout, result.stderr)
    return result.ok, log_tail


async def _compose_stop(
    server: Server, stack: AgentStack
) -> tuple[bool, str]:
    """Run ``docker compose stop -t 0`` for the stack — a *force* stop.

    ``-t 0`` gives the container zero grace before SIGKILL, so this is an
    immediate hard stop (the container exits 137 / SIGKILL), versus the
    graceful 10-second SIGTERM of a bare ``stop``. Unlike :func:`_compose_down`
    we keep the container and the stack files in place, so the stack can be
    brought straight back up with redeploy / run-now.

    A container stopped this way is NOT resurrected by its
    ``restart: unless-stopped`` policy — Docker treats a manual stop as
    "leave it down until something explicitly starts it again" (the policy
    only fires on a crash / daemon restart, not after an operator stop). The
    stop is scoped to THIS stack's compose project (``-p``), so a sibling
    stack on the same host — e.g. a different agent's bot — is untouched.

    We pass ``-f <stack_dir>/docker-compose.yml`` for the same CWD-independence
    reason as :func:`_compose_down`, and ``check=False`` so a non-zero exit
    (e.g. "no such service" on an already-removed stack) surfaces in the log
    tail rather than raising an opaque exception.
    """
    run_command = _import_ssh_commands()
    cmd = (
        f"docker compose -p {_shell_quote(stack.compose_project)} "
        f"-f {_shell_quote(stack.stack_dir + '/docker-compose.yml')} "
        f"stop -t 0"
    )
    # 60s is plenty: -t 0 SIGKILLs immediately, no graceful drain.
    result = await run_command(server, cmd, timeout=60.0, check=False)
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
    # ``run_results`` holds the bot's scheduled-run markers (issue #62 —
    # the bot's scheduler.py writes ``scheduled_run_<uuid>.json`` here
    # per cron fire and the mgmt UI's scheduled_run_ingestor SFTPs them
    # back). MUST exist on the host before compose-up, otherwise the
    # bind mount lands on a Docker-created (root-owned) directory which
    # the non-root SSH user can't read for ingestion.
    mkdir_cmd = (
        f"mkdir -p {sd}/logs {sd}/order_results {sd}/.cache {sd}/run_results"
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
    force_recreate: bool = False,
) -> StackActionResult:
    """Shared implementation for provision and redeploy.

    Both actions follow the same shape:

    1. Acquire the per-server advisory lock on a *dedicated* side-channel
       session (so the lock survives the regular ``db`` session's commits).
    2. Flip status to ``provisioning``.
    3. Render all five files.
    4. (Optional) mkdir + touch on the remote — only on first provision.
    5. SFTP-push the files.
    6. ``docker compose up -d`` (with ``--force-recreate --pull always``
       on redeploy — see :func:`_compose_up` for the rationale).
    7. Flip status to ``up`` (success) or ``down`` (failure) and audit.
    8. Release the advisory lock in ``finally``.

    The only difference is the audit action name, whether we prepare
    directories, and whether we force-recreate the container. Keeping the
    logic in one place means "redeploy" can never accidentally drift from
    "provision" in subtle ways.
    """
    stack = await get_stack(db, stack_id)
    if stack is None:
        raise LookupError(f"stack {stack_id} not found")

    lock_key = _compose_lock_key(stack.server_id)

    # Hold the advisory lock on a dedicated session for the WHOLE duration
    # of the action — including all SFTP and docker-compose work that runs
    # AFTER the ``db`` session commits the ``provisioning`` status flip.
    # If we held it on ``db``, the first commit would release the
    # (transaction-scoped) lock and concurrent provisions on the same
    # server could interleave.
    async with AsyncSessionLocal() as lock_session:
        if not await try_acquire_session_lock(lock_session, lock_key):
            raise RuntimeError(
                "another compose op is in flight for this server, retry"
            )
        try:
            server = await db.get(Server, stack.server_id)
            if server is None:
                raise LookupError(
                    f"server {stack.server_id} for stack "
                    f"{stack.id} is missing"
                )

            # Step 2: mark as in-progress so the UI can show a spinner.
            before = _public_snapshot(stack)
            stack.status = "provisioning"
            await db.commit()
            # Re-load the stack into the session so subsequent attribute
            # access on ``stack`` works (commit expires attributes by
            # default — but our session has expire_on_commit=False, so
            # this is a no-op safety net).
            await db.refresh(stack)

            log_tail = ""
            try:
                files = await _render_all_files(db, stack)
                if prepare_dirs:
                    await _prepare_remote_dirs(server, stack)
                await _push_files(server, stack, files)
                ok, log_tail = await _compose_up(
                    server, stack, force_recreate=force_recreate
                )
            except Exception as exc:
                # Any failure before/during compose flips status to
                # ``down`` so the next admin action knows we left the
                # remote in an indeterminate state. We commit the status
                # update before re-raising so the router can render the
                # failure with a fresh DB row.
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
        finally:
            # Always release the session-scoped lock — even on exception
            # paths — so a transient failure doesn't pin the lock until
            # the connection eventually times out. A best-effort release
            # is correct here: if release itself fails (e.g. the
            # connection went away), the lock will be cleaned up by
            # Postgres when the underlying session closes.
            try:
                await release_session_lock(lock_session, lock_key)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.exception(
                    "failed to release advisory lock key=%s", lock_key
                )


async def force_stop_stack(
    db: AsyncSession,
    stack_id: UUID,
    *,
    actor_id: Optional[UUID],
) -> StackActionResult:
    """Force-stop a single stack's container(s) and mark the row ``down``.

    Operator-facing "force kill": ``docker compose stop -t 0`` (immediate
    SIGKILL — see :func:`_compose_stop`), scoped to THIS stack's compose
    project only, so a different agent's bot on the same host is untouched.
    The DB row is flipped to ``down`` and a ``stack.force_kill`` audit row is
    written, so the dashboard reflects reality immediately rather than waiting
    for the stack-health worker's next poll to discover the container is gone.

    Reversible: a redeploy or run-now brings the stack back up (the container
    and the on-disk stack files are left in place).

    Serialised on the same per-server advisory lock as provision / redeploy /
    deprovision, so a force-kill can't race a deploy on the same host. Raises
    ``RuntimeError`` if that lock is busy (the caller surfaces a "retry"), and
    propagates ``SSHError`` if the host is unreachable — in which case the row
    is left unchanged (we never claim "down" when we couldn't reach the host).
    """
    stack = await get_stack(db, stack_id)
    if stack is None:
        raise LookupError(f"stack {stack_id} not found")

    lock_key = _compose_lock_key(stack.server_id)
    async with AsyncSessionLocal() as lock_session:
        if not await try_acquire_session_lock(lock_session, lock_key):
            raise RuntimeError(
                "another compose op is in flight for this server, retry"
            )
        try:
            server = await db.get(Server, stack.server_id)
            if server is None:
                raise LookupError(
                    f"server {stack.server_id} for stack "
                    f"{stack.id} is missing"
                )

            before = _public_snapshot(stack)
            # SSHError here propagates out (host unreachable) BEFORE we touch
            # the row — so a force-kill we couldn't deliver doesn't lie about
            # the status.
            ok, log_tail = await _compose_stop(server, stack)

            # The intent is "down" regardless of the compose exit code: if the
            # container was already gone, ``stop`` is a harmless no-op and we
            # still want the row to read down.
            stack.status = "down"
            await _write_audit(
                db,
                actor_id=actor_id,
                action="stack.force_kill",
                target_id=stack.id,
                before=before,
                after=_public_snapshot(stack),
            )
            await db.commit()
            await db.refresh(stack)

            message = (
                "stack force-stopped"
                if ok
                else "compose stop returned non-zero exit (row marked down)"
            )
            return StackActionResult(
                ok=ok,
                stack_id=stack.id,
                status=stack.status,
                message=message,
                log_tail=log_tail,
            )
        finally:
            try:
                await release_session_lock(lock_session, lock_key)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.exception(
                    "failed to release advisory lock key=%s", lock_key
                )


async def force_stop_stacks(
    db: AsyncSession,
    stack_ids: list[UUID],
    *,
    actor_id: Optional[UUID],
) -> list[StackActionResult]:
    """Force-stop several stacks, best-effort — backs the "force kill all".

    Each stack is stopped independently: a lock-busy / SSH / lookup failure on
    one stack is captured as a failed :class:`StackActionResult` and the loop
    continues, so one unreachable host can't block killing the rest. Stacks
    are handled sequentially — the per-server advisory lock already serialises
    same-host work and a bulk force-kill is rare + not latency-sensitive.
    """
    results: list[StackActionResult] = []
    for sid in stack_ids:
        try:
            results.append(
                await force_stop_stack(db, sid, actor_id=actor_id)
            )
        except (RuntimeError, SSHError, LookupError) as exc:
            # Reset the shared session so a half-finished attempt can't leave
            # it in a failed-transaction state and poison the next stack's
            # commit.
            await db.rollback()
            results.append(
                StackActionResult(
                    ok=False,
                    stack_id=sid,
                    status="down",
                    message=f"force-kill failed: {exc}",
                    log_tail=str(exc),
                )
            )
    return results


async def _maybe_reconcile_agent(
    db: AsyncSession, stack_id: UUID, actor_id: Optional[UUID]
) -> None:
    """Auto-balance the stack's agent + scale locust *before* a deploy.

    Runs BEFORE :func:`_do_compose_action` (not inside it) because the reused
    ``move_customer`` / ``push_*_for_stack`` helpers acquire the same per-server
    compose lock — nesting would self-conflict. Gated by ``enable_autobalance``;
    best-effort (any failure is logged and swallowed so it can never block the
    deploy). The render step that follows still auto-scales the deploying stack's
    own locust from ``enable_locust_autoscale``, independent of this.
    """
    if not _parse_bool(await _read_setting(db, "enable_autobalance", "true")):
        return
    try:
        multiplier = int(
            await _read_setting(db, "autobalance_users_multiplier", "3")
        )
    except (TypeError, ValueError):
        multiplier = 3
    try:
        stack = await get_stack(db, stack_id)
        if stack is None:
            return
        from app.services import autobalance  # noqa: WPS433

        await autobalance.reconcile_agent(
            db,
            stack.agent_id,
            actor_id,
            apply=True,
            enable_balance=True,
            multiplier=max(1, multiplier),
            skip_locust_push_for=stack_id,
        )
    except Exception:  # noqa: BLE001 — never block a deploy
        # Reset the shared session so a half-finished reconcile can't leave it in
        # a failed-transaction state and break the deploy's own commit below.
        await db.rollback()
        logger.exception(
            "autobalance reconcile for stack %s failed", stack_id
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
    await _maybe_reconcile_agent(db, stack_id, actor_id)
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
    """Re-render config + force-recreate the container for an existing stack.

    Useful when an admin:

    * Changes the OCR URL or agent image tag in the settings table.
    * Wants to be sure recent config-file edits are visible to the running
      container — single-file bind mounts pin the destination inode at
      ``docker run`` time and any non-in-place file replacement on the
      host leaves the container reading frozen content. (Our SFTP helper
      writes in place to avoid this, but ``--force-recreate`` is the
      belt-and-braces fix for any container created BEFORE that change
      landed, or for files edited via tools that atomic-rename on save.)
    * Wants a "reset this stack" knob to recover from a weird container
      state.
    * Has previously deprovisioned this stack and now wants to bring it
      back up — deprovision removes the stack directory, so we must
      ``mkdir -p`` it again before the SFTP push, otherwise the writes
      fail with ENOENT.

    ``mkdir -p`` and ``touch`` are idempotent (no-op when the targets
    already exist), so we always run them. The cost is two extra SSH
    round-trips per redeploy — negligible compared to ``--force-recreate
    --pull always`` (see :func:`_compose_up`).
    """
    await _maybe_reconcile_agent(db, stack_id, actor_id)
    return await _do_compose_action(
        db,
        stack_id,
        actor_id,
        audit_action="stack.redeploy",
        prepare_dirs=True,
        force_recreate=True,
    )


async def deprovision_stack(
    db: AsyncSession,
    stack_id: UUID,
    actor_id: Optional[UUID],
) -> StackActionResult:
    """Tear down the stack and remove its row.

    Sequence:

    1. Acquire the per-server advisory lock on a dedicated session.
    2. Flip status to ``deprovisioning``.
    3. ``docker compose down --volumes --remove-orphans``. Capture the result.
    4. ``rm -rf <stack_dir>`` — but ONLY if the path passes two safety
       checks (see below). This is the most dangerous remote command in the
       module; the checks are belt-and-braces. Capture the result.
    5. If either compose-down OR rm failed, return ``ok=False`` with the
       captured log_tail and LEAVE the row in place (status stays
       ``deprovisioning``). The admin can retry once they know why.
    6. Only if both succeeded: delete the row, audit ``stack.deprovision``,
       return ``ok=True``.

    ``rm -rf`` safety:

    * ``stack.stack_dir`` MUST equal ``<server.base_dir>/<agent_id>`` exactly
      OR start with that prefix followed by ``"/"``. A naive prefix check
      would let ``<base>/<agent_id>-evil`` slip past — using
      ``startswith(prefix + "/")`` rules that out.
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

    lock_key = _compose_lock_key(stack.server_id)

    async with AsyncSessionLocal() as lock_session:
        if not await try_acquire_session_lock(lock_session, lock_key):
            raise RuntimeError(
                "another compose op is in flight for this server, retry"
            )
        try:
            server = await db.get(Server, stack.server_id)
            if server is None:
                raise LookupError(
                    f"server {stack.server_id} for stack "
                    f"{stack.id} is missing"
                )

            before = _public_snapshot(stack)
            stack.status = "deprovisioning"
            await db.commit()
            await db.refresh(stack)

            # Step 3: compose down. Capture ok + log so we can surface a
            # real failure to the admin rather than silently deleting the
            # row out from under a container that's still running.
            log_tail = ""
            try:
                compose_ok, log_tail = await _compose_down(server, stack)
            except Exception as exc:
                # If SSH itself fails (auth error, host unreachable) we
                # can't safely remove the dir or the row — log + re-raise
                # so the admin can retry once the server is reachable.
                # Status stays ``deprovisioning`` so the UI can show "in
                # progress" until then.
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

            # If compose-down reported a non-zero exit, do NOT proceed to
            # rm/delete — we'd be leaving a still-running container with no
            # DB record of it, which is much harder to recover from than a
            # stuck "deprovisioning" row. Return ok=False and let the admin
            # decide.
            if not compose_ok:
                return StackActionResult(
                    ok=False,
                    stack_id=stack_id,
                    status="deprovisioning",
                    message=(
                        "docker compose down failed; row not removed "
                        "(retry deprovision or investigate manually)"
                    ),
                    log_tail=log_tail,
                )

            # Step 4: rm -rf, gated by the per-stack scope check. The
            # actual rule lives in ``_assert_rm_target_in_scope`` so that
            # the (security-critical) sibling-prefix logic is unit-testable
            # without standing up a full deprovision flow.
            _assert_rm_target_in_scope(
                stack.stack_dir, server.base_dir or "", stack.agent_id
            )

            run_command = _import_ssh_commands()
            rm_cmd = f"rm -rf {_shell_quote(stack.stack_dir)}"
            # check=False so we capture stderr but don't raise — but we DO
            # consult result.ok below to decide whether the deprovision
            # actually succeeded (vs. the previous behaviour, which
            # silently swallowed failures and deleted the row anyway).
            rm_result = await run_command(
                server, rm_cmd, timeout=60.0, check=False
            )
            rm_log = _tail_log(rm_result.stdout, rm_result.stderr)
            if rm_log:
                log_tail = (
                    log_tail + "\n--- rm ---\n" + rm_log
                    if log_tail
                    else rm_log
                )

            if not rm_result.ok:
                # Compose came down cleanly but the directory cleanup
                # failed (e.g. permissions, disk error). Leave the row in
                # place so the admin can retry — deleting now would lose
                # the record of where the leftover files live.
                return StackActionResult(
                    ok=False,
                    stack_id=stack_id,
                    status="deprovisioning",
                    message=(
                        "rm -rf of stack_dir failed; row not removed "
                        "(retry deprovision or remove the directory "
                        "manually)"
                    ),
                    log_tail=log_tail,
                )

            # Step 6: both teardown steps succeeded — safe to delete the
            # row. First demote this stack's customers to 'pending' so they
            # don't become invisible orphans (active + stack_id NULL) when the
            # FK nulls their stack_id — they land back in the admin inbox for
            # re-placement instead of silently dropping out of config.ini.
            await _demote_stack_customers(db, stack.id, actor_id)
            # Audit BEFORE delete so the target_id reference still makes sense
            # to a future reader.
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
        finally:
            try:
                await release_session_lock(lock_session, lock_key)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.exception(
                    "failed to release advisory lock key=%s", lock_key
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


# ---------------------------------------------------------------------------
# Phase 4: customer-driven config.ini push / preview
# ---------------------------------------------------------------------------


async def push_config_ini_for_stack(
    db: AsyncSession,
    stack_id: UUID,
    actor_id: Optional[UUID],
) -> StackActionResult:
    """Render ``config.ini`` from the stack's current customers and SFTP-push it.

    Runs under the per-server compose advisory lock so it can't race the
    provision/redeploy/deprovision actions. This is the function that
    customer-mutation endpoints call after persisting a change, so the change
    is immediately reflected in the trading bot's config.

    On non-provisioned stacks (status='down' or never deployed), we still
    render + push the file; the bot will pick it up on the next start. We
    deliberately do NOT restart the container — the bot's ``scheduler.py``
    re-reads ``scheduler_config.json`` every second and ``locustfile_new.py``
    reads ``config.ini`` only at run start, so the new sections will be
    picked up by the next scheduled run automatically.

    Raises:
        LookupError: stack or server row missing.
        RuntimeError: another compose op is in flight for this server.
        SSHError: an SFTP failure bubbles up so the caller can show it.
        PathOutOfScopeError: the rendered remote path falls outside
            ``stack.stack_dir`` (defence in depth — should never happen
            because we construct the path from ``stack.stack_dir`` itself).
    """
    stack = await get_stack(db, stack_id)
    if stack is None:
        raise LookupError(f"stack not found: {stack_id}")
    server = await db.get(Server, stack.server_id)
    if server is None:
        raise LookupError(f"server not found: {stack.server_id}")

    lock_key = _compose_lock_key(server.id)
    async with AsyncSessionLocal() as lock_session:
        if not await try_acquire_session_lock(lock_session, lock_key):
            raise RuntimeError(
                "another compose op is in flight for this server"
            )
        try:
            ctx = await _build_render_context(db, stack)
            new_content = render_config_ini(ctx)
            remote_path = f"{stack.stack_dir}/config.ini"
            # Belt-and-braces stack-scope guard (the SFTP helper also has a
            # per-server guard, but the per-stack one closes the misaligned
            # stack_dir hole — see _assert_stack_path_in_scope).
            _assert_stack_path_in_scope(stack, remote_path)
            sftp_atomic_write, _ = _import_sftp()
            await sftp_atomic_write(server, remote_path, new_content)
            await _write_audit(
                db,
                actor_id=actor_id,
                action="stack.push_config_ini",
                target_id=stack.id,
                before=None,
                after={"path": remote_path, "len": len(new_content)},
            )
            await db.commit()
            return StackActionResult(
                ok=True,
                stack_id=stack.id,
                status=stack.status,
                message="config.ini pushed",
            )
        finally:
            try:
                await release_session_lock(lock_session, lock_key)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.exception(
                    "failed to release compose lock key=%s", lock_key
                )


@dataclass
class FleetPushItem:
    """Per-stack outcome of a fleet-wide config.ini push."""

    stack_id: UUID
    agent_id: UUID
    server_id: UUID
    server_name: str
    ok: bool
    status: str  # "pushed" | "lock_busy" | "host_down" | "error"
    message: str


@dataclass
class FleetPushResult:
    """Aggregate outcome of :func:`push_config_ini_to_all_stacks`."""

    total: int
    succeeded: int
    failed: int
    items: list[FleetPushItem] = field(default_factory=list)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


_FLEET_PUSH_TIMEOUT_S = 25.0


async def push_config_ini_to_all_stacks(
    db: AsyncSession,
    actor_id: Optional[UUID],
    *,
    only_stack_ids: Optional[Sequence[UUID]] = None,
) -> FleetPushResult:
    """Push the freshly-rendered ``config.ini`` to EVERY stack (best-effort).

    This is what makes a DB-pushed ``[runtime]`` change (broker/market-data
    hosts, exir fee, time windows, OCR pool) land on the whole fleet in seconds —
    no CI, no image, no container recreate. Each stack reuses
    :func:`push_config_ini_for_stack` (its per-server compose advisory lock, its
    in-place inode-preserving SFTP write).

    Concurrency model: pushes are **sequential within a server** (the per-server
    lock self-serializes anyway — two concurrent pushes to one host would make
    one fail ``lock_busy``) and **parallel across servers**. Each push runs in
    its OWN ``AsyncSessionLocal`` (asyncpg connections aren't safe to share
    across ``asyncio.gather`` branches); the passed ``db`` is used only for the
    initial reads + the fleet audit. A per-stack timeout keeps one hung host from
    stalling the whole result.

    Returns a :class:`FleetPushResult` with a per-stack status so the operator
    can confirm the disaster change actually landed (and retry the failures).
    """
    started = datetime.now(timezone.utc)

    # Warm the broker-family cache ONCE up front: config_ini's family_of()
    # silently mislabels exir → ephoenix on a cold cache, which would corrupt
    # every exir customer's section across the whole fleet in one push.
    try:
        from app.services.brokers.registry import warm_family_cache  # noqa: WPS433
        await warm_family_cache(db)
    except Exception:  # noqa: BLE001 — a cache-warm failure must not abort the push
        logger.exception("fleet push: warm_family_cache failed (continuing)")

    stacks = await list_stacks(db)
    if only_stack_ids is not None:
        wanted = {sid for sid in only_stack_ids}
        stacks = [s for s in stacks if s.id in wanted]

    # Resolve server display names once (for the status table).
    server_names: dict[UUID, str] = {}
    for sid in {s.server_id for s in stacks}:
        srv = await db.get(Server, sid)
        server_names[sid] = srv.name if srv is not None else str(sid)

    by_server: dict[UUID, list[AgentStack]] = {}
    for s in stacks:
        by_server.setdefault(s.server_id, []).append(s)

    async def _push_one(stack: AgentStack) -> FleetPushItem:
        name = server_names.get(stack.server_id, str(stack.server_id))

        def _item(ok: bool, status: str, message: str) -> FleetPushItem:
            return FleetPushItem(
                stack_id=stack.id, agent_id=stack.agent_id,
                server_id=stack.server_id, server_name=name,
                ok=ok, status=status, message=message,
            )

        try:
            async with AsyncSessionLocal() as push_db:
                await asyncio.wait_for(
                    push_config_ini_for_stack(push_db, stack.id, actor_id),
                    timeout=_FLEET_PUSH_TIMEOUT_S,
                )
            return _item(True, "pushed", "config.ini pushed")
        except asyncio.TimeoutError:
            return _item(False, "host_down", "timed out")
        except RuntimeError as exc:  # compose lock busy
            return _item(False, "lock_busy", str(exc))
        except SSHError as exc:
            return _item(False, "host_down", str(exc))
        except LookupError as exc:
            return _item(False, "error", str(exc))
        except Exception as exc:  # noqa: BLE001 — one stack must never abort the rest
            logger.exception("fleet push: stack=%s failed", stack.id)
            return _item(False, "error", str(exc))

    async def _push_server(server_stacks: list[AgentStack]) -> list[FleetPushItem]:
        results: list[FleetPushItem] = []
        for st in server_stacks:
            results.append(await _push_one(st))
        return results

    groups = await asyncio.gather(*(_push_server(v) for v in by_server.values()))
    items = [it for group in groups for it in group]
    items.sort(key=lambda i: (i.server_name, str(i.stack_id)))
    succeeded = sum(1 for i in items if i.ok)
    finished = datetime.now(timezone.utc)

    # One fleet-level audit row (the per-stack pushes already audit themselves).
    try:
        db.add(AuditLog(
            actor_user_id=actor_id,
            action="stack.push_config_ini_fleet",
            target_type="stack",
            target_id="fleet",
            before_json=None,
            after_json={"total": len(items), "succeeded": succeeded,
                        "failed": len(items) - succeeded},
        ))
        await db.commit()
    except Exception:  # noqa: BLE001 — audit is best-effort
        logger.exception("fleet push: audit write failed")
        await db.rollback()

    return FleetPushResult(
        total=len(items), succeeded=succeeded, failed=len(items) - succeeded,
        items=items, started_at=started, finished_at=finished,
    )


async def render_config_ini_for_stack_preview(
    db: AsyncSession,
    stack_id: UUID,
) -> tuple[str, str]:
    """Return ``(current_remote_content, new_rendered_content)``.

    Used by the admin's assign/move flows for the diff-preview UI. On SFTP
    read failure (file missing, server unreachable, stack not yet
    provisioned), the current half is the empty string. The new half is
    always derived from the current DB state.

    No advisory lock is taken — this is a read-only preview and the caller
    is happy with a "best-effort" snapshot. If a write is in flight on the
    same server, the current half may reflect either the pre- or post-write
    state, which is acceptable for a UI preview.
    """
    stack = await get_stack(db, stack_id)
    if stack is None:
        raise LookupError(f"stack not found: {stack_id}")
    server = await db.get(Server, stack.server_id)
    if server is None:
        raise LookupError(f"server not found: {stack.server_id}")

    ctx = await _build_render_context(db, stack)
    new_content = render_config_ini(ctx)

    _, sftp_read_text = _import_sftp()
    current = ""
    try:
        current = await sftp_read_text(
            server, f"{stack.stack_dir}/config.ini"
        )
    except SSHError as exc:
        # Stack may simply not be provisioned yet, or the file may have been
        # removed by hand. Surface an empty "current" half so the diff UI
        # can show "this is a fresh push".
        logger.info(
            "preview: sftp_read_text failed stack=%s: %s", stack_id, exc
        )
    return (current, new_content)


# ---------------------------------------------------------------------------
# Phase 5: scheduler / locust push + preview
# ---------------------------------------------------------------------------


async def _push_rendered_json_for_stack(
    db: AsyncSession,
    stack_id: UUID,
    actor_id: Optional[UUID],
    *,
    filename: str,
    render_fn,
    audit_action: str,
    flash_message: str,
) -> StackActionResult:
    """Shared body for the two Phase-5 push helpers.

    Both ``push_scheduler_config_for_stack`` and
    ``push_locust_config_for_stack`` do the exact same dance — only the
    filename, renderer, audit action, and success message differ. Keeping
    the shared structure in one private helper means the lock + audit + SFTP
    semantics can never drift between the two public functions.

    See :func:`push_config_ini_for_stack` for the rationale behind each
    step (per-server advisory lock on a dedicated session, stack-scope
    guard, audit before commit, best-effort lock release in ``finally``).
    """
    stack = await get_stack(db, stack_id)
    if stack is None:
        raise LookupError(f"stack not found: {stack_id}")
    server = await db.get(Server, stack.server_id)
    if server is None:
        raise LookupError(f"server not found: {stack.server_id}")

    lock_key = _compose_lock_key(server.id)
    async with AsyncSessionLocal() as lock_session:
        if not await try_acquire_session_lock(lock_session, lock_key):
            raise RuntimeError(
                "another compose op is in flight for this server"
            )
        try:
            ctx = await _build_render_context(db, stack)
            new_content = render_fn(ctx)
            remote_path = f"{stack.stack_dir}/{filename}"
            _assert_stack_path_in_scope(stack, remote_path)
            sftp_atomic_write, _ = _import_sftp()
            await sftp_atomic_write(server, remote_path, new_content)
            await _write_audit(
                db,
                actor_id=actor_id,
                action=audit_action,
                target_id=stack.id,
                before=None,
                after={"path": remote_path, "len": len(new_content)},
            )
            await db.commit()
            return StackActionResult(
                ok=True,
                stack_id=stack.id,
                status=stack.status,
                message=flash_message,
            )
        finally:
            try:
                await release_session_lock(lock_session, lock_key)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.exception(
                    "failed to release compose lock key=%s", lock_key
                )


async def push_scheduler_config_for_stack(
    db: AsyncSession,
    stack_id: UUID,
    actor_id: Optional[UUID],
) -> StackActionResult:
    """Render ``scheduler_config.json`` from the stack's current
    ``scheduler_jobs`` rows and SFTP-push it under the per-server compose
    advisory lock.

    The bot's ``scheduler.py`` re-reads this file every ~1 second of its
    poll loop, so the change takes effect within ~1s of the push — no
    container restart needed.

    Raises:
        LookupError: stack or server row missing.
        RuntimeError: another compose op is in flight for this server.
        SSHError: an SFTP failure bubbles up so the caller can show it.
        PathOutOfScopeError: defensive — should never happen because we
            construct the path from ``stack.stack_dir``.
    """
    return await _push_rendered_json_for_stack(
        db,
        stack_id,
        actor_id,
        filename="scheduler_config.json",
        render_fn=render_scheduler_config,
        audit_action="stack.push_scheduler_config",
        flash_message="scheduler_config.json pushed",
    )


async def push_locust_config_for_stack(
    db: AsyncSession,
    stack_id: UUID,
    actor_id: Optional[UUID],
) -> StackActionResult:
    """Render ``locust_config.json`` from the stack's locust_config row and
    SFTP-push it under the per-server compose advisory lock.

    Effective on the NEXT scheduled trade run — the bot's
    ``locustfile_new.py`` reads this file at process start, not
    continuously. The push itself is immediate; only the *effect* waits for
    the next scheduled run.

    Raises:
        LookupError: stack or server row missing.
        RuntimeError: another compose op is in flight for this server.
        SSHError: an SFTP failure bubbles up so the caller can show it.
        PathOutOfScopeError: defensive — see push_scheduler_config_for_stack.
    """
    return await _push_rendered_json_for_stack(
        db,
        stack_id,
        actor_id,
        filename="locust_config.json",
        render_fn=render_locust_config,
        audit_action="stack.push_locust_config",
        flash_message="locust_config.json pushed",
    )


async def render_scheduler_config_for_stack_preview(
    db: AsyncSession,
    stack_id: UUID,
) -> tuple[str, str]:
    """Return ``(current_remote_content, new_rendered_content)`` for the
    scheduler diff-preview UI.

    No redaction is applied — ``scheduler_config.json`` carries no secrets
    (no passwords, no tokens). On SFTP read failure (file missing, server
    unreachable, stack not yet provisioned) the current half is the empty
    string.
    """
    stack = await get_stack(db, stack_id)
    if stack is None:
        raise LookupError(f"stack not found: {stack_id}")
    server = await db.get(Server, stack.server_id)
    if server is None:
        raise LookupError(f"server not found: {stack.server_id}")

    ctx = await _build_render_context(db, stack)
    new_content = render_scheduler_config(ctx)

    _, sftp_read_text = _import_sftp()
    current = ""
    try:
        current = await sftp_read_text(
            server, f"{stack.stack_dir}/scheduler_config.json"
        )
    except SSHError as exc:
        logger.info(
            "preview: sftp_read_text failed stack=%s file=scheduler_config.json: %s",
            stack_id,
            exc,
        )
    return (current, new_content)


async def render_locust_config_for_stack_preview(
    db: AsyncSession,
    stack_id: UUID,
) -> tuple[str, str]:
    """Return ``(current_remote_content, new_rendered_content)`` for the
    locust diff-preview UI.

    No redaction is applied — ``locust_config.json`` carries no secrets.
    On SFTP read failure the current half is the empty string.
    """
    stack = await get_stack(db, stack_id)
    if stack is None:
        raise LookupError(f"stack not found: {stack_id}")
    server = await db.get(Server, stack.server_id)
    if server is None:
        raise LookupError(f"server not found: {stack.server_id}")

    ctx = await _build_render_context(db, stack)
    new_content = render_locust_config(ctx)

    _, sftp_read_text = _import_sftp()
    current = ""
    try:
        current = await sftp_read_text(
            server, f"{stack.stack_dir}/locust_config.json"
        )
    except SSHError as exc:
        logger.info(
            "preview: sftp_read_text failed stack=%s file=locust_config.json: %s",
            stack_id,
            exc,
        )
    return (current, new_content)


__all__ = [
    "deprovision_stack",
    "find_or_create_stack",
    "get_stack",
    "list_stacks",
    "provision_stack",
    "push_config_ini_for_stack",
    "push_locust_config_for_stack",
    "push_scheduler_config_for_stack",
    "redeploy_stack",
    "render_config_ini_for_stack_preview",
    "render_locust_config_for_stack_preview",
    "render_scheduler_config_for_stack_preview",
    "stack_files_preview",
]
