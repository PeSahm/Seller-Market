"""SSH helpers specific to the run lifecycle.

Today: best-effort process-tree kill inside a trading bot container.
Used by the admin "Force kill" recovery action to clean up any python
left running on the trading host when the mgmt UI's executor task is
gone (e.g. api container restart mid-run).
"""

from __future__ import annotations

import logging
import shlex

from app.models.servers import Server
from app.services.ssh.commands import run_command

logger = logging.getLogger(__name__)


async def remote_kill_run_processes(
    server: Server,
    container_name: str,
    *,
    patterns: tuple[str, ...] = ("cache_warmup", "locustfile"),
    timeout: float = 15.0,
) -> int:
    """SIGKILL any matching process inside ``container_name`` via the host.

    Docker on Linux puts container processes in the host's PID namespace
    (unless `--pid` was used), so ``docker top`` lists PIDs that the host
    can ``kill -9`` directly. That's how we yank an orphan python when
    the SSH-channel-close path didn't (because the python was blocked
    in a sleep loop and never tried to write).

    Args:
        server: Trading host record (the one with SSH creds).
        container_name: The bot container name, e.g.
            ``sm-agent-<stack_uuid>-bot``.
        patterns: Substring matches against the COMMAND column of
            ``docker top``. Default covers both cache_warmup and locust.
            Must be non-empty — an empty tuple would build ``grep -E ''``
            which matches EVERY process in the container and we'd kill
            unrelated work. We fail closed with a log line and ``0``.
        timeout: SSH-side total timeout.

    Returns:
        Count of PIDs we attempted to ``kill -9``. ``0`` is the normal
        outcome when the container isn't running, no matching process
        exists, or the SSH-side hit any error (recovery helper — never
        raises). Counts are exact — the function snapshots PIDs first,
        kills them, and returns ``len(snapshot)``.
    """
    if not patterns:
        # `grep -E ''` matches every line, which would kill every process
        # in the container — not what an empty patterns argument should
        # mean. Refuse explicitly so a misconfigured caller can't blow
        # up the bot.
        logger.warning(
            "remote_kill_run_processes: empty patterns for %s — refusing to "
            "build an unfiltered kill pipeline",
            container_name,
        )
        return 0

    pat = "|".join(shlex.quote(p) for p in patterns).replace("'", "")
    cn = shlex.quote(container_name)

    # First pipeline: print one matching PID per line — gives us the
    # snapshot we'll feed to xargs AND lets us report an exact count.
    # The wrapping ``|| true`` swallows the failure when ``docker top``
    # errors (container stopped) so the shell exits 0 and we read an
    # empty stdout.
    list_cmd = (
        f"sh -c \"docker top {cn} 2>/dev/null "
        f"| awk 'NR>1 {{print}}' "
        f"| grep -E {shlex.quote(pat)} "
        f"| awk '{{print \\$2}}' "
        f"|| true\""
    )
    try:
        listed = await run_command(server, list_cmd, timeout=timeout)
    except Exception:  # noqa: BLE001 — recovery path, swallow everything
        logger.exception(
            "remote_kill_run_processes: SSH failure listing PIDs for %s on %s",
            container_name, getattr(server, "name", "?"),
        )
        return 0

    pids: list[str] = []
    for line in (listed.stdout or "").splitlines():
        line = line.strip()
        # docker top's PID column is purely digits on every supported
        # platform — anything else is junk and we drop it.
        if line.isdigit():
            pids.append(line)

    if not pids:
        logger.info(
            "remote_kill_run_processes: %s has no matching processes",
            container_name,
        )
        return 0

    # Second pipeline: SIGKILL the snapshot. Done as a separate command
    # so the count we return is the actual number we attempted to kill,
    # not a 0/1 sentinel.
    kill_cmd = "sh -c " + shlex.quote(
        "kill -9 " + " ".join(pids) + " 2>/dev/null || true"
    )
    try:
        await run_command(server, kill_cmd, timeout=timeout)
    except Exception:  # noqa: BLE001
        logger.exception(
            "remote_kill_run_processes: SSH failure killing PIDs %s in %s",
            pids, container_name,
        )
        # We still return the count we *attempted* — operator sees the
        # intent in the audit log even if delivery failed.

    logger.info(
        "remote_kill_run_processes: %s — issued SIGKILL to %d PID(s): %s",
        container_name, len(pids), ",".join(pids),
    )
    return len(pids)


__all__ = ["remote_kill_run_processes"]
