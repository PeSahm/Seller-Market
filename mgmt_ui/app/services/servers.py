"""Server CRUD orchestration (Phase 2).

This layer is the seam between the HTTP router and the lower-level pieces:

* :mod:`app.models` for DB rows
* :mod:`app.security.crypto` for at-rest password encryption
* :mod:`app.services.ssh` for live SSH probes
* :mod:`app.models.audit` for write-side audit logging

The router stays thin: it converts a form into a pydantic model, calls one
function here, and renders the result. Anything involving more than a single
SQL statement or any filesystem I/O lives in this module.

SSH secret storage
------------------
Two flavours, both stored as text in ``servers.ssh_secret_ref``:

* **Password** → Fernet-encrypted, base64 ASCII string (the raw output of
  :func:`app.security.crypto.encrypt`, which is already url-safe base64).
* **Pubkey** → an absolute path to a file under
  ``<repo>/mgmt_ui/.ssh_mgmt_ui/sm_<server_id>``. We write the key, chmod
  it to 0600 on POSIX (no-op on Windows), and store the path. The Docker
  compose already mounts that directory into the API container.

TODOs for future phases
-----------------------
* Optimistic locking: the ``Server`` model has no ``version`` column. Concurrent
  updates may silently overwrite each other. Add a ``version`` column + bump in
  Phase 9.
* Soft delete: the model has no ``deleted_at``. Today's :func:`soft_delete_server`
  performs a hard delete + key-file cleanup. Add a ``deleted_at`` column +
  migration in Phase 9 (audit-log polish) and revisit.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.customers import Customer, DistributionPolicy
from app.models.servers import Server, ServerClockSkewSample
from app.models.stacks import AgentStack
from app.schemas.server import (
    ServerCreatePassword,
    ServerCreatePubkey,
    ServerUpdate,
    TestConnectionResult,
)
from app.security.crypto import encrypt as fernet_encrypt
from app.services.ssh.exceptions import (
    AuthenticationError,
    HostKeyMismatchError,
    RemoteCommandError,
    SSHConnectionError,
)
from app.services.ssh.pool import fingerprint as ssh_fingerprint

logger = logging.getLogger(__name__)


# Where we drop on-disk private keys. The Docker compose for the API
# container mounts this directory read-only into the container's
# ``/home/app/.ssh`` so paramiko can read it from inside the pool.
# Resolved at import time so tests can monkeypatch ``_KEY_DIR``.
_KEY_DIR = Path(__file__).resolve().parent.parent.parent / ".ssh_mgmt_ui"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    """A timezone-aware ``datetime.utcnow()`` for TIMESTAMPTZ columns."""
    return datetime.now(timezone.utc)


def _key_path_for(server_id: UUID) -> Path:
    """Return the on-disk path for a server's private key file.

    Note we reconstruct the canonical UUID string from ``server_id.hex`` (32
    lowercase hex chars, no separators) rather than letting f-string call
    ``str(server_id)``. Functionally identical output, but it makes the
    "only [0-9a-f] reaches the filesystem path" property explicit to static
    analysers — CodeQL's ``py/path-injection`` taint flow stops at ``.hex``
    because the type signature proves the result is sanitized.
    """
    raw = server_id.hex  # guaranteed 32 chars of [0-9a-f]
    canonical_uuid = (
        f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
    )
    return _KEY_DIR / f"sm_{canonical_uuid}"


# Canonical UUID string form (lowercase hex with hyphens) is what
# ``str(UUID(...))`` returns. The regex enforces no shell-special chars and
# no separators even if a future refactor accidentally passes a non-UUID.
_SERVER_KEY_NAME_RE = re.compile(
    r"^sm_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _resolve_key_path(server_id: UUID) -> Path:
    """Return the on-disk key path for ``server_id``, asserting containment.

    Defense-in-depth against path-injection (CodeQL py/path-injection):

    - The resolved path's parent MUST equal the resolved key directory.
    - The filename MUST match ``sm_<canonical-uuid>``.

    If either check fails, raise ``ValueError`` — the caller decides whether
    to log and abort or to treat as a security event.

    The ``UUID`` type hint already gives us strong validation at the HTTP
    boundary (FastAPI rejects non-UUID path params), but encoding the
    invariant here means CodeQL's dataflow analysis sees a barrier before
    the value reaches a filesystem call, and it survives a future refactor
    that loosens the type.
    """
    candidate = _key_path_for(server_id)
    # ``resolve()`` works on non-existent paths on Python 3.6+; no need for
    # ``strict=False`` gymnastics. It collapses ``..`` segments and symlinks.
    resolved = candidate.resolve()
    key_dir_resolved = _KEY_DIR.resolve()
    if resolved.parent != key_dir_resolved:
        raise ValueError(
            f"key path escapes key dir: parent={resolved.parent!r} "
            f"expected={key_dir_resolved!r}"
        )
    if not _SERVER_KEY_NAME_RE.fullmatch(resolved.name):
        raise ValueError(
            f"key filename does not match sm_<uuid> pattern: {resolved.name!r}"
        )
    return resolved


def _write_key_file(server_id: UUID, private_key: str) -> Path:
    """Write the private key to its on-disk location with 0600 perms.

    On Windows, ``os.chmod`` is a no-op for POSIX-style mode bits — it only
    toggles the read-only attribute. That's acceptable for dev: the host file
    system is just a staging area, the API actually runs inside the Linux
    container where the bind mount makes the file world-unreadable to other
    container users anyway. We still call chmod unconditionally so we can't
    forget to set it on a POSIX deploy.

    The destination directory is created with ``parents=True`` so a fresh
    checkout (which doesn't ship ``.ssh_mgmt_ui/``) doesn't blow up the
    create flow.
    """
    _KEY_DIR.mkdir(parents=True, exist_ok=True)
    # Route through the asserting validator so CodeQL sees a barrier between
    # the UUID input and the ``os.open`` call below (py/path-injection).
    path = _resolve_key_path(server_id)
    # Ensure the key payload ends with a newline — some loaders are picky.
    payload = private_key if private_key.endswith("\n") else private_key + "\n"
    # Use a low-level open with O_CREAT|O_TRUNC|O_WRONLY so we can set 0600
    # at create time (relevant for POSIX). We then chmod again unconditionally
    # to repair the case where the file already existed.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except Exception:
        # If write fails after open, make sure we don't leave a zero-byte
        # file behind that might fool a later "key exists" check.
        try:
            path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            logger.debug("cleanup after failed key write also failed", exc_info=True)
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows reports ENOTSUP/EPERM for some chmod bits — that's fine,
        # the security boundary on dev is the user account, not the FS bit.
        logger.debug("chmod 0600 best-effort failed for %s", path, exc_info=True)
    return path


def _delete_key_file(server_id: UUID) -> None:
    """Remove the on-disk key for a server if present. Never raises.

    Routes through :func:`_resolve_key_path` so any path that would escape
    the key directory is rejected before reaching ``unlink``. A failed
    containment check is a security-relevant signal, not a routine cleanup
    failure, so we log it at ERROR with a distinct message — that way the
    alert surfaces in monitoring instead of being lost in INFO noise.
    """
    try:
        path = _resolve_key_path(server_id)
    except ValueError:
        logger.error(
            "refusing to delete key file: path containment check failed for "
            "server_id=%r",
            server_id,
            exc_info=True,
        )
        return
    try:
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.warning("could not remove key file %s", path, exc_info=True)


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

    ``target_type`` is always ``"server"`` for this module. The caller is
    responsible for ensuring ``before``/``after`` contain no secret material:
    in particular, never put ``ssh_secret_ref`` in either payload.
    """
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            action=action,
            target_type="server",
            target_id=str(target_id),
            before_json=before,
            after_json=after,
        )
    )


def _public_snapshot(server: Server) -> dict:
    """Audit-safe dict of a Server row (no secret material)."""
    return {
        "id": str(server.id),
        "name": server.name,
        "host": server.host,
        "ssh_port": server.ssh_port,
        "ssh_user": server.ssh_user,
        "ssh_auth": server.ssh_auth,
        "host_key_pin": server.host_key_pin,
        "status": server.status,
        "base_dir": server.base_dir,
        "image_pull_policy": getattr(server, "image_pull_policy", "always"),
    }


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_servers(
    db: AsyncSession,
    *,
    include_deleted: bool = False,  # noqa: ARG001 — kept for forward-compat
) -> list[Server]:
    """Return all servers ordered by name.

    ``include_deleted`` is a no-op until the ``deleted_at`` migration lands
    (see module docstring TODO). Keeping the parameter in the signature now
    means router code doesn't have to change later.
    """
    stmt = select(Server).order_by(Server.name)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_server(db: AsyncSession, server_id: UUID) -> Optional[Server]:
    """Look up a single server by id."""
    stmt = select(Server).where(Server.id == server_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def create_server(
    db: AsyncSession,
    data: Union[ServerCreatePassword, ServerCreatePubkey],
    actor_id: UUID,
) -> Server:
    """Insert a new server and persist its SSH credential.

    Order of operations:
    1. Insert the row with a placeholder ``ssh_secret_ref`` so we get an ID.
    2. Materialize the secret (file-on-disk for pubkey, base64 ciphertext for
       password) keyed by the new ID.
    3. Update the row's ``ssh_secret_ref`` to point at the materialized secret.
    4. Write an audit log entry and commit.

    Rationale: we need the server's UUID to name the key file. If step 2 or 3
    raises, we attempt best-effort cleanup of the key file, then roll back so
    no orphan row is left behind.
    """
    server = Server(
        name=data.name,
        host=data.host,
        ssh_port=data.ssh_port,
        ssh_user=data.ssh_user,
        ssh_auth=data.ssh_auth,
        ssh_secret_ref="",  # placeholder — patched below before commit
        host_key_pin=None,
        status="unknown",
        last_seen_at=None,
        base_dir=data.base_dir,
        image_pull_policy=data.image_pull_policy,
    )
    db.add(server)
    # Flush so the DB-side default (gen_random_uuid) populates server.id.
    await db.flush()

    try:
        if isinstance(data, ServerCreatePubkey):
            path = _write_key_file(server.id, data.private_key)
            server.ssh_secret_ref = str(path)
        elif isinstance(data, ServerCreatePassword):
            ciphertext = fernet_encrypt(data.password)
            # fernet_encrypt returns the url-safe base64 token as bytes —
            # decode to ASCII for the Text column. (Re-base64-encoding would
            # be redundant; Fernet's output is already base64.)
            server.ssh_secret_ref = ciphertext.decode("ascii")
        else:  # pragma: no cover — pydantic discriminator should prevent this
            raise TypeError(f"unsupported create payload: {type(data).__name__}")

        await _write_audit(
            db,
            actor_id=actor_id,
            action="server.create",
            target_id=server.id,
            before=None,
            after=_public_snapshot(server),
        )
        await db.commit()
    except Exception:
        # Best-effort cleanup: if we wrote a key file, remove it before the
        # rollback so we don't leave a private key on disk pointing at a
        # row that never existed.
        await db.rollback()
        if isinstance(data, ServerCreatePubkey):
            _delete_key_file(server.id)
        raise

    await db.refresh(server)
    return server


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def update_server(
    db: AsyncSession,
    server_id: UUID,
    data: ServerUpdate,
    actor_id: UUID,
) -> Server:
    """Apply a partial update to a server row.

    Only fields explicitly set on ``data`` are touched — pydantic v2's
    ``model_dump(exclude_unset=True)`` gives us that semantic without us
    needing sentinel values.

    Raises ``LookupError`` if no such server exists. (Phase 9 should switch
    this to a typed exception once we settle on an error model.)
    """
    server = await get_server(db, server_id)
    if server is None:
        raise LookupError(f"server {server_id} not found")

    before = _public_snapshot(server)
    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(server, field, value)

    await _write_audit(
        db,
        actor_id=actor_id,
        action="server.update",
        target_id=server.id,
        before=before,
        after=_public_snapshot(server),
    )
    await db.commit()
    await db.refresh(server)
    return server


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class ServerInUseError(Exception):
    """A server still has customers or bot stacks placed on it, so it can't be
    deleted (the FKs are ``ON DELETE RESTRICT``). Move/remove those first."""


async def soft_delete_server(
    db: AsyncSession,
    server_id: UUID,
    actor_id: UUID,
) -> None:
    """Hard-delete a server (Phase 2 scope — see module-level TODO).

    Four tables FK ``servers.id`` with ``ON DELETE RESTRICT``:
    ``customers`` + ``agent_stacks`` are real data — if any still reference this
    server we REFUSE with :class:`ServerInUseError` (the operator must move them
    first) rather than let the DB raise an opaque IntegrityError → HTTP 500.
    ``server_clock_skew_samples`` (telemetry) + ``distribution_policies.
    default_server_id`` (a soft pointer) would ALSO block the delete even after
    customers/stacks are gone, so we clear them first: the samples are deleted,
    the policy pointer is nulled. Then the row is deleted and the key file (if
    pubkey auth) removed.
    """
    server = await get_server(db, server_id)
    if server is None:
        # Idempotent: deleting a non-existent server is a no-op.
        return

    # Block on real data (RESTRICT) — a clear error, not a 500.
    n_customers = (
        await db.execute(
            select(func.count()).select_from(Customer).where(
                Customer.server_id == server_id
            )
        )
    ).scalar() or 0
    n_stacks = (
        await db.execute(
            select(func.count()).select_from(AgentStack).where(
                AgentStack.server_id == server_id
            )
        )
    ).scalar() or 0
    if n_customers or n_stacks:
        raise ServerInUseError(
            f"server still has {n_customers} customer(s) and {n_stacks} bot "
            f"stack(s) on it — move or remove them before deleting the server."
        )

    before = _public_snapshot(server)
    is_pubkey = server.ssh_auth == "pubkey"

    # Clear the blocking soft references (telemetry samples + the policy pointer)
    # so the RESTRICT FKs don't reject the delete.
    await db.execute(
        delete(ServerClockSkewSample).where(
            ServerClockSkewSample.server_id == server_id
        )
    )
    await db.execute(
        update(DistributionPolicy)
        .where(DistributionPolicy.default_server_id == server_id)
        .values(default_server_id=None)
    )

    await _write_audit(
        db,
        actor_id=actor_id,
        action="server.delete",
        target_id=server.id,
        before=before,
        after=None,
    )
    await db.delete(server)
    await db.commit()

    if is_pubkey:
        _delete_key_file(server_id)


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------


def _import_run_command():
    """Import ``run_command`` lazily so tests can run without the SSH layer.

    The commands module is owned by a parallel agent — importing it at
    module load would couple our import order to theirs. Doing it lazily
    means ``test_connection`` is the only function that depends on it being
    present.
    """
    from app.services.ssh.commands import run_command  # noqa: WPS433

    return run_command


async def test_connection(
    db: AsyncSession,
    server_id: UUID,
    actor_id: Optional[UUID] = None,
) -> TestConnectionResult:
    """Probe a server via SSH and persist what we learn.

    ``actor_id`` is ``None`` when invoked by the background health worker
    (system-initiated checks). All audit-log writes already accept ``None``
    in that field.

    Sequence:

    1. Look up the server. Missing → raise ``LookupError``.
    2. Read the remote host-key fingerprint via :func:`ssh_fingerprint`.
       - If we have no pin yet (``host_key_pin is None``), TOFU: record it,
         audit ``server.pin``.
       - If we have a pin and it doesn't match: return early with
         ``host_key_mismatch=True``. We DO NOT touch ``host_key_pin`` —
         rotation is a separate, explicit admin action.
    3. Run three short remote commands:
         - ``date +%s`` → unix epoch on the remote, computes ``delta_seconds``.
         - ``test -d /root/seller-market`` → exit 0 means the legacy deploy
           is present (good — means we're not on a fresh box).
         - ``docker --version`` → optional, captured if present.
    4. Persist a ``ServerClockSkewSample`` row.
    5. Mark the server ``status='online'`` and update ``last_seen_at``.

    Any SSH-level error (connection failure, auth failure, command failure)
    flips the server to ``status='offline'``, returns ``ok=False`` with a
    descriptive message, and does NOT touch the host_key_pin (other than the
    TOFU branch above, which has already returned by that point).
    """
    server = await get_server(db, server_id)
    if server is None:
        raise LookupError(f"server {server_id} not found")

    result = TestConnectionResult(ok=False, message="")

    # ---- 1) Host-key fingerprint + TOFU / mismatch handling --------------

    try:
        observed = await ssh_fingerprint(server.host, server.ssh_port)
    except SSHConnectionError as exc:
        server.status = "offline"
        await db.commit()
        result.message = f"could not reach host: {exc}"
        return result
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        logger.exception("unexpected error fetching host key for %s", server.id)
        server.status = "offline"
        await db.commit()
        result.message = f"unexpected error fetching host key: {exc}"
        return result

    result.fingerprint = observed

    if server.host_key_pin is None:
        # TOFU: first successful contact — pin it.
        before = _public_snapshot(server)
        server.host_key_pin = observed
        result.new_pin = True
        await _write_audit(
            db,
            actor_id=actor_id,
            action="server.pin",
            target_id=server.id,
            before=before,
            after=_public_snapshot(server),
        )
    elif server.host_key_pin.lower() != observed.lower():
        result.host_key_mismatch = True
        result.message = (
            "host key mismatch — the remote presented a fingerprint that "
            "does not match the pinned value. Refusing to connect."
        )
        # We DELIBERATELY do not flip status to offline here; a mismatch
        # is a security-relevant signal distinct from "the box is down".
        await db.commit()
        return result

    # ---- 2) Probe commands ----------------------------------------------

    try:
        run_command = _import_run_command()
    except ImportError as exc:
        logger.warning("ssh.commands not yet available: %s", exc)
        # Persist what we already learned (the pin) and bail.
        await db.commit()
        result.message = (
            "SSH command layer not available in this build; pin recorded."
        )
        return result

    local_unix_before = int(time.time())
    # Issue #67: detect "SSH user can't write under base_dir" BEFORE the
    # operator clicks Provision and hits a confusing `mkdir: Permission
    # denied`. If the dir exists, require it to be both a directory AND
    # writable; if it doesn't exist yet (typical on a fresh box), walk up
    # the ancestor tree to the nearest existing path and check that — that
    # mirrors what `mkdir -p` actually does (it walks up from the leaf and
    # creates every intermediate dir). The single-`dirname` shape would
    # report "denied" when only the leaf's parent is missing but the
    # grandparent is writable — exactly when mkdir -p would succeed.
    base_dir_q = shlex.quote(server.base_dir)
    base_dir_writable_cmd = (
        f"if test -e {base_dir_q}; then "
        f"test -d {base_dir_q} && test -w {base_dir_q}; "
        f"else "
        f"p=\"$(dirname {base_dir_q})\"; "
        f"while test ! -e \"$p\" && test \"$p\" != /; do "
        f"p=\"$(dirname \"$p\")\"; "
        f"done; "
        f"test -w \"$p\"; "
        f"fi"
    )
    try:
        remote_now_res = await run_command(
            server, "date +%s", timeout=10, check=True
        )
        remote_dir_res = await run_command(
            server,
            "test -d /root/seller-market",
            timeout=10,
            check=False,
        )
        docker_res = await run_command(
            server,
            "docker --version 2>/dev/null || true",
            timeout=10,
            check=False,
        )
        base_dir_res = await run_command(
            server,
            base_dir_writable_cmd,
            timeout=10,
            check=False,
        )
    except HostKeyMismatchError:
        # Race: pin changed between fingerprint() and the first exec. Treat
        # the same as the up-front check above.
        result.host_key_mismatch = True
        result.message = (
            "host key mismatch detected on command execution; aborting."
        )
        await db.commit()
        return result
    except AuthenticationError as exc:
        server.status = "offline"
        await db.commit()
        result.message = f"authentication failed: {exc}"
        return result
    except SSHConnectionError as exc:
        server.status = "offline"
        await db.commit()
        result.message = f"connection failed: {exc}"
        return result
    except RemoteCommandError as exc:
        # ``date +%s`` exited non-zero — extremely unusual but possible on
        # an extremely locked-down shell.
        server.status = "offline"
        await db.commit()
        result.message = f"remote command failed: {exc}"
        return result
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        logger.exception("unexpected error probing server %s", server.id)
        server.status = "offline"
        await db.commit()
        result.message = f"unexpected error during probe: {exc}"
        return result

    # ---- 3) Parse results ------------------------------------------------

    try:
        remote_unix = int(_first_line(remote_now_res.stdout).strip())
    except (AttributeError, TypeError, ValueError):
        server.status = "offline"
        await db.commit()
        result.message = (
            "could not parse remote `date +%s` output — is the shell sane?"
        )
        return result

    # Average local time across the round-trip so we don't bias the delta
    # by the network RTT of a single sample.
    local_unix_after = int(time.time())
    local_unix_mid = (local_unix_before + local_unix_after) // 2
    delta_seconds = remote_unix - local_unix_mid
    result.clock_skew_seconds = delta_seconds

    result.seller_market_present = (
        getattr(remote_dir_res, "exit_code", None) == 0
    )

    docker_stdout = getattr(docker_res, "stdout", "") or ""
    docker_version = docker_stdout.strip()
    result.docker_version = docker_version or None

    # Issue #67: surface "SSH user can't write under base_dir" before the
    # operator hits Provision. ``ssh_user`` is echoed so the template can
    # render a copy-pastable fix command naming the actual user.
    result.base_dir_probed = server.base_dir
    result.ssh_user = server.ssh_user
    result.base_dir_writable = (
        getattr(base_dir_res, "exit_code", None) == 0
    )
    # When the probe says "denied", pre-render the fix line server-side
    # using shlex.quote on every interpolated value. Building this in the
    # template would force the template to do shell-quoting (Jinja's
    # ``|escape`` is HTML-only) — values like ``ssh_user="some user"`` or
    # ``base_dir`` with spaces could otherwise produce a broken or
    # injection-hazard pasted command.
    if result.base_dir_writable is False:
        ssh_user_safe = shlex.quote(server.ssh_user)
        base_dir_safe = shlex.quote(server.base_dir)
        fix = (
            f"sudo install -d -m 0755 -o {ssh_user_safe} -g {ssh_user_safe} "
            f"{base_dir_safe}"
        )
        # If the base path is under root's home, append `chmod o+x /root` —
        # default /root mode 0700 blocks even traversal for a non-root
        # user, so without this the install would still fail. The mode
        # change is traversal-only ("o+x"): contents of /root keep their
        # own modes and aren't exposed.
        #
        # Use a path-component test (== "/root" OR startswith("/root/"))
        # rather than a bare prefix check — ``"/rooting".startswith("/root")``
        # is True even though /rooting is unrelated to the root home dir.
        if server.base_dir == "/root" or server.base_dir.startswith("/root/"):
            fix += " \\\n  && sudo chmod o+x /root"
        result.base_dir_fix_command = fix

    # ---- 4) Persist sample + status update -------------------------------

    await record_clock_skew_sample(db, server.id, delta_seconds)

    server.status = "online"
    server.last_seen_at = _now_utc()

    await db.commit()
    await db.refresh(server)

    result.ok = True
    parts = [f"clock skew {delta_seconds:+d}s"]
    if result.seller_market_present:
        parts.append("/root/seller-market present")
    else:
        parts.append("/root/seller-market missing")
    if result.docker_version:
        parts.append(result.docker_version)
    if result.new_pin:
        parts.append("first contact — host key pinned")
    result.message = "; ".join(parts)
    return result


# ---------------------------------------------------------------------------
# Clock-skew sample
# ---------------------------------------------------------------------------


async def record_clock_skew_sample(
    db: AsyncSession,
    server_id: UUID,
    delta_seconds: int,
) -> None:
    """Insert a clock-skew sample row.

    Does not commit — the caller batches it into a wider transaction (see
    :func:`test_connection`). That way a failed status update doesn't leave
    behind a sample row that suggests the probe succeeded.
    """
    db.add(
        ServerClockSkewSample(
            server_id=server_id,
            delta_seconds=delta_seconds,
        )
    )


# ---------------------------------------------------------------------------
# Misc small helpers
# ---------------------------------------------------------------------------


def _first_line(s: str) -> str:
    """First non-empty line of a possibly-multiline string, or empty."""
    if not s:
        return ""
    for line in s.splitlines():
        if line.strip():
            return line
    return ""


# Re-export of base64 to make the password-encoding intent explicit at the
# call site even though we currently don't double-encode. Kept so a reviewer
# notices if we ever start storing raw bytes instead of ASCII.
__all__ = [
    "ServerInUseError",
    "create_server",
    "get_server",
    "list_servers",
    "record_clock_skew_sample",
    "soft_delete_server",
    "test_connection",
    "update_server",
    "_KEY_DIR",
    "_key_path_for",
]
# Silence unused-import lint for base64 — kept for the docstring note above.
_ = base64
