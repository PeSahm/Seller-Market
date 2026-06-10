"""Bot container entrypoint (#110): scheduler (background) + auto-sell monitor (foreground).

The mgmt-rendered stack runs the bot as the in-container scheduler loop. This
entrypoint keeps that behaviour (the scheduler still fires cache_warmup /
run_trading on cron) AND adds the long-running auto-sell monitor alongside it —
the same shape as ``simple_config_bot.main()`` (scheduler in a daemon thread, a
foreground loop). With no armed instructions (or no ``MARKET_DATA_URL``) it
behaves exactly like the scheduler-only bootstrap, so it's a safe drop-in.

The agent-stack compose ``command:`` is switched to ``python -u bot_entrypoint.py``
by the mgmt renderer (``compose_yaml.py``); ``MARKET_DATA_URL`` is injected there.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

logger = logging.getLogger("bot_entrypoint")


def _start_scheduler() -> None:
    """Run the existing JobScheduler loop in a daemon thread (unchanged behaviour)."""
    try:
        from scheduler import JobScheduler

        config_path = os.environ.get("SCHEDULER_CONFIG", "/app/scheduler_config.json")
        sched = JobScheduler(config_path)
        threading.Thread(target=sched.run, daemon=True, name="JobScheduler").start()
        logger.info("scheduler started (config=%s)", config_path)
    except Exception:  # noqa: BLE001 — never let the scheduler bring down the container
        logger.exception("failed to start scheduler")


def _idle() -> None:
    while True:
        time.sleep(3600)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    _start_scheduler()

    from auto_sell_monitor import AutoSellMonitor, load_auto_sell_targets

    config_ini = os.environ.get("CONFIG_INI", "/app/config.ini")
    market_data_url = os.environ.get("MARKET_DATA_URL", "")

    if not market_data_url:
        # No feed wired (env change needs a redeploy anyway) → scheduler-only.
        logger.warning("MARKET_DATA_URL unset — auto-sell disabled (scheduler-only)")
        _idle()

    # ALWAYS run the supervisor when a feed is configured — even with zero armed
    # targets. It reads config.ini live and arms the first watch the operator
    # adds WITHOUT a container restart (the old code idled forever on 0 targets).
    targets = load_auto_sell_targets(config_ini)
    logger.info("starting auto-sell monitor (hot-reload) — %d armed at boot", len(targets))
    AutoSellMonitor(targets, market_data_url=market_data_url).run_supervised(config_ini)


if __name__ == "__main__":
    main()
