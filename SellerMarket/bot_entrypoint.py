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

import runtime_config

logger = logging.getLogger("bot_entrypoint")


def _market_data_url() -> str:
    """Auto-sell feed URL: DB-pushed ``[runtime] market_data_url`` > env
    ``MARKET_DATA_URL`` > '' (auto-sell off). Read once at start — enabling/
    changing the feed endpoint still needs a monitor restart (documented
    limitation; the local sidecar URL essentially never changes)."""
    return runtime_config.get("market_data_url", "") or os.environ.get("MARKET_DATA_URL", "")


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


def _mofid_schedule_config() -> dict:
    """The Mofid scheduler's single-job config, with the run-time (login +
    draft-creation start) read LIVE from ``[runtime]`` on every call.

    The base ``JobScheduler`` re-reads its config each loop, so returning a fresh
    dict here means editing ``mofid_run_time`` in Settings applies on the next
    open with NO redeploy — matching the fire-window / draft-count knobs (which
    ``mofid_firer`` / ``mofid_adapter`` read at fire time). A mid-day change can't
    double-fire: ``run_mofid``'s per-(account,isin) day latch guards that.
    """
    return {
        "enabled": True,
        "jobs": [{
            "name": "run_mofid",
            "time": runtime_config.get("mofid_run_time", "08:44:00"),
            "command": "python run_mofid.py",
            "enabled": True,
        }],
    }


def _start_mofid_scheduler() -> None:
    """Launch a SECOND, independent JobScheduler for the Mofid firer when this
    stack has Mofid BUY sections.

    Mofid can't ride the locust spam (its 1500-requests/hour cap) and can't share
    the single sequential main scheduler — ``run_mofid`` would serialize with the
    blocking ``run_trading`` subprocess and miss its open window. So it gets its
    OWN scheduler thread that fires ``run_mofid.py`` at the open, CONCURRENT with
    (and never blocking) ``run_trading``. ``run_mofid`` itself pre-creates the
    drafts then batch-sends them in the server-time-synced window.

    The schedule's run-time is read LIVE from ``[runtime]`` each loop
    (``_mofid_schedule_config``), so ALL Mofid firing settings — drafts, fire
    window AND the draft-creation start — apply on the next open with no redeploy.

    Gated on Mofid sections being present → a byte-identical no-op on every
    non-Mofid stack. Adding the FIRST Mofid customer to a running stack still
    needs a redeploy to launch this thread (the gate runs once at container start).
    """
    try:
        config_ini = os.environ.get("CONFIG_INI", "/app/config.ini")
        import run_mofid

        targets = run_mofid.mofid_buy_targets(config_ini)
        if not targets:
            return  # no Mofid BUY sections → don't launch the second scheduler

        from scheduler import JobScheduler

        class _MofidScheduler(JobScheduler):
            # Override load_config so the run-time comes from [runtime] LIVE each
            # loop instead of a file baked at startup (→ instant, no redeploy).
            def load_config(self):
                return _mofid_schedule_config()

        sched = _MofidScheduler("")  # config_file unused — load_config overridden
        threading.Thread(target=sched.run, daemon=True, name="MofidScheduler").start()
        logger.info(
            "mofid scheduler started — %d section(s), run_time live from [runtime] "
            "(instant, independent of run_trading)", len(targets),
        )
    except Exception:  # noqa: BLE001 — never let the Mofid scheduler crash the container
        logger.exception("failed to start mofid scheduler")


def _idle() -> None:
    while True:
        time.sleep(3600)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    _start_scheduler()
    _start_mofid_scheduler()

    from auto_sell_monitor import AutoSellMonitor, load_auto_sell_targets

    config_ini = os.environ.get("CONFIG_INI", "/app/config.ini")
    market_data_url = _market_data_url()

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
