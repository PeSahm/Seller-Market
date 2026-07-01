"""Hermetic tests for bot_entrypoint config-precedence helpers."""
from __future__ import annotations

import bot_entrypoint
import runtime_config


def test_market_data_url_runtime_first(monkeypatch):
    runtime_config.reset_cache()
    monkeypatch.setenv("MARKET_DATA_URL", "http://env:8077")
    monkeypatch.setattr(runtime_config, "_snapshot", lambda: {})
    assert bot_entrypoint._market_data_url() == "http://env:8077"   # env when no runtime
    monkeypatch.setattr(runtime_config, "_snapshot",
                        lambda: {"market_data_url": "http://rt:8077"})
    assert bot_entrypoint._market_data_url() == "http://rt:8077"    # runtime wins


def test_market_data_url_empty_default(monkeypatch):
    monkeypatch.delenv("MARKET_DATA_URL", raising=False)
    monkeypatch.setattr(runtime_config, "_snapshot", lambda: {})
    assert bot_entrypoint._market_data_url() == ""


# ----------------------------------------- independent Mofid scheduler (gating)
def test_mofid_scheduler_not_started_without_sections(monkeypatch):
    """No Mofid BUY sections → no second scheduler (byte-identical no-op on
    every non-Mofid stack)."""
    import run_mofid

    monkeypatch.setattr(run_mofid, "mofid_buy_targets", lambda _p: [])
    threads = []
    monkeypatch.setattr(
        bot_entrypoint.threading, "Thread",
        lambda *a, **k: threads.append(k.get("name")),
    )
    bot_entrypoint._start_mofid_scheduler()
    assert threads == []


def test_mofid_scheduler_started_with_sections(monkeypatch):
    """With a Mofid BUY section → a MofidScheduler thread launches."""
    import run_mofid
    import scheduler as scheduler_mod

    monkeypatch.setattr(
        run_mofid, "mofid_buy_targets", lambda _p: [("s1", {"isin": "IRO1DPAK0001"})]
    )

    class _FakeSched:
        def __init__(self, path):
            self.path = path

        def run(self):  # pragma: no cover - never actually run in the test
            pass

    monkeypatch.setattr(scheduler_mod, "JobScheduler", _FakeSched)

    started = []

    class _FakeThread:
        def __init__(self, *a, **k):
            started.append(k.get("name"))

        def start(self):
            pass

    monkeypatch.setattr(bot_entrypoint.threading, "Thread", _FakeThread)

    bot_entrypoint._start_mofid_scheduler()
    assert started == ["MofidScheduler"]


def test_mofid_schedule_config_reads_run_time_live(monkeypatch):
    """The schedule's run_time is read LIVE from [runtime] each call, so editing
    mofid_run_time in Settings applies on the next open with no redeploy."""
    monkeypatch.setattr(runtime_config, "_snapshot", lambda: {})  # nothing set
    cfg = bot_entrypoint._mofid_schedule_config()
    assert cfg["enabled"] is True
    job = cfg["jobs"][0]
    assert job["name"] == "run_mofid"
    assert job["command"] == "python run_mofid.py"
    assert job["time"] == "08:44:00"  # default when unset
    monkeypatch.setattr(
        runtime_config, "_snapshot", lambda: {"mofid_run_time": "08:43:00"}
    )
    assert bot_entrypoint._mofid_schedule_config()["jobs"][0]["time"] == "08:43:00"
