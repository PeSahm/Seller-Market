"""SSH helpers specific to the run lifecycle.

Today: best-effort process-tree kill inside a trading bot container.
Used by the admin "Force kill" recovery action to clean up any python
left running on the trading host when the mgmt UI's executor task is
gone (e.g. api container restart mid-run).
"""

from __future__ import annotations

import logging
import shlex
from typing import Optional

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
        timeout: SSH-side total timeout.

    Returns:
        Number of PIDs we attempted to kill. ``0`` if the container
        isn't running or nothing matched — both are normal outcomes
        and not errors.

    Never raises — every failure path is logged and converted to ``0``.
    This is a recovery helper; an SSH problem here MUST NOT block the
    higher-level DB cleanup.
    """
    # `docker top <container>` writes a ps-style table with PIDs in
    # column 2 from the HOST's namespace. Pipe through grep/awk and
    # SIGKILL anything matching `patterns`. The whole thing is wrapped
    # in `|| true` so docker-top failing (container stopped) doesn't
    # propagate as a non-zero exit.
    pat = "|".join(shlex.quote(p) for p in patterns).replace("'", "")
    # Avoid sh-injection of the container name: shlex.quote it.
    cn = shlex.quote(container_name)
    # The double `2>/dev/null` keeps stderr clean — `docker top` shouts
    # if the container isn't running, and grep is fine to be silent.
    cmd = (
        f"sh -c \"docker top {cn} 2>/dev/null "
        f"| grep -E {shlex.quote(pat)} "
        f"| awk '{{print \\$2}}' "
        f"| xargs -r kill -9 2>/dev/null; "
        f"docker top {cn} 2>/dev/null "
        f"| grep -cE {shlex.quote(pat)} "
        f"|| echo 0\""
    )

    try:
        result = await run_command(server, cmd, timeout=timeout)
    except Exception:  # noqa: BLE001 — recovery path, swallow everything
        logger.exception(
            "remote_kill_run_processes: SSH failure for %s on server %s",
            container_name, getattr(server, "name", "?"),
        )
        return 0

    # The trailing `docker top | grep -c` reports survivors — useful
    # signal for the operator, but not actionable. The KILLS happened
    # in the first pipeline. Count of attempted kills isn't directly
    # exposed, but we can grep before-kill if we want it precise. For
    # v1 just return 1 if the remote shell ran at all and there's
    # output, else 0.
    survivors_text = (result.stdout or "").strip().splitlines()
    survivors = 0
    for line in reversed(survivors_text):
        line = line.strip()
        if line.isdigit():
            survivors = int(line)
            break
    if result.exit_code != 0:
        logger.warning(
            "remote_kill_run_processes: shell exited %d on %s/%s — stderr=%r",
            result.exit_code, getattr(server, "name", "?"),
            container_name, (result.stderr or "")[:200],
        )
    if survivors:
        logger.warning(
            "remote_kill_run_processes: %d matching process(es) STILL alive in "
            "%s after SIGKILL — container may be using a separate PID namespace",
            survivors, container_name,
        )
    else:
        logger.info(
            "remote_kill_run_processes: %s cleaned up (no matching survivors)",
            container_name,
        )
    return 1  # best-effort indicator that the remote ran


__all__ = ["remote_kill_run_processes"]
