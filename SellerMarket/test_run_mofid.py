"""Hermetic tests for ``run_mofid.mofid_buy_targets`` — the shared gate that
decides which sections fire AND whether ``bot_entrypoint`` launches the
independent Mofid scheduler. No network, no DB; importing run_mofid is
side-effect-free (logging setup lives in ``_setup_logging``, called from main).
"""
from __future__ import annotations

import textwrap

import run_mofid


def _write_config(tmp_path, body: str) -> str:
    p = tmp_path / "config.ini"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


def test_selects_only_mofid_buy_sections(tmp_path):
    cfg = _write_config(tmp_path, """
        [runtime]
        market_data_url = http://x

        [a_b_t_mofid_dpak_s1]
        username = 4580090306
        password = p
        broker = mofid
        broker_family = mofid
        isin = IRO1DPAK0001
        side = 1

        [a_b_t_mofid_dzah_s1]
        username = 4580090306
        password = p
        broker = mofid
        broker_family = mofid
        isin = IRO1DZAH0001
        side = 1

        [a_b_t_karamad_dpak_s1]
        username = 4580090306
        password = p
        broker = karamad
        broker_family = ephoenix
        isin = IRO1DPAK0001
        side = 1

        [a_b_t_mofid_sell_s2]
        username = 4580090306
        password = p
        broker = mofid
        broker_family = mofid
        isin = IRO1ZZZZ0001
        side = 2

        [a_b_t_mofid_autosell]
        username = 4580090306
        password = p
        broker = mofid
        broker_family = mofid
        isin = IRO1WWWW0001
        side = 1
        auto_sell_only = true
        auto_sell_threshold = 1000
    """)
    targets = run_mofid.mofid_buy_targets(cfg)
    # Only the two Mofid BUY rows: NOT [runtime] (no username), NOT karamad
    # (ephoenix family), NOT the SELL (side=2), NOT the auto-sell-only watch.
    assert sorted(n for n, _ in targets) == [
        "a_b_t_mofid_dpak_s1", "a_b_t_mofid_dzah_s1",
    ]
    assert sorted(s["isin"] for _, s in targets) == ["IRO1DPAK0001", "IRO1DZAH0001"]


def test_empty_when_no_mofid_sections(tmp_path):
    cfg = _write_config(tmp_path, """
        [a_b_t_karamad_s1]
        username = u
        broker = karamad
        broker_family = ephoenix
        isin = IRO1AAAA0001
        side = 1
    """)
    assert run_mofid.mofid_buy_targets(cfg) == []


def test_missing_file_is_empty(tmp_path):
    assert run_mofid.mofid_buy_targets(str(tmp_path / "nope.ini")) == []


def test_bad_side_is_skipped_not_raised(tmp_path):
    cfg = _write_config(tmp_path, """
        [a_b_t_mofid_bad]
        username = u
        broker = mofid
        broker_family = mofid
        isin = IRO1AAAA0001
        side = notanint
    """)
    assert run_mofid.mofid_buy_targets(cfg) == []


# ------------------------------------------- cross-restart idempotency latch
def test_fire_latch_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(run_mofid, "_RUN_RESULTS_DIR", str(tmp_path))
    acct, isin = "4580090306", "IRO1DPAK0001"
    assert run_mofid._fired_today(acct, isin) is False
    run_mofid._mark_fired_today(acct, isin)
    assert run_mofid._fired_today(acct, isin) is True
    # a different isin / account is independent (per-(account,isin)-per-day)
    assert run_mofid._fired_today(acct, "IRO1DZAH0001") is False
    assert run_mofid._fired_today("9999999999", isin) is False


def test_fire_section_skips_when_already_fired(monkeypatch):
    """A latched (account, isin) must NOT log in or create a draft again — the
    core guard against a restart-in-window double BUY."""
    monkeypatch.setattr(run_mofid, "_fired_today", lambda _a, _i: True)
    called = []
    monkeypatch.setattr(run_mofid, "get_adapter", lambda *a, **k: called.append(1))
    out = run_mofid.fire_section(
        "s1", {"username": "u", "broker": "mofid", "isin": "X", "side": "1"}
    )
    assert out is False
    assert called == []  # never reached get_adapter → no login, no draft, no POST
