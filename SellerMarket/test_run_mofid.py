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
