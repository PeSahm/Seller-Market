"""Unit tests for the load-balance / locust auto-scale core.

The balancing math (`plan_moves`, `compute_locust_targets`) and the render-time
auto-scale are pure functions — exercised directly with no DB.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.services.autobalance import (
    PlannedMove,
    _hysteresis_threshold,
    _load_customer_sections,
    compute_locust_targets,
    plan_moves,
)
from app.services.rendering import CustomerRow, LocustConfigRow, StackRenderContext
from app.services.rendering.locust_config import render_locust_config


# ---------------------------------------------------------------------------
# compute_locust_targets
# ---------------------------------------------------------------------------


def test_locust_targets_scale_3x_sections():
    assert compute_locust_targets(26) == (78, 26)  # Hamid's Tebyan stack
    assert compute_locust_targets(23) == (69, 23)


def test_locust_targets_floor_for_small_stacks():
    # 3×2 = 6 is below the floor of 10 → users clamps up to 10; spawn = sections.
    assert compute_locust_targets(2) == (10, 2)
    assert compute_locust_targets(0) == (10, 1)  # spawn floors at 1


def test_locust_targets_respect_explicit_floor():
    # A manually-set users value acts as a floor.
    assert compute_locust_targets(5, floor_users=100) == (100, 5)


def test_locust_targets_clamp_to_schema_caps():
    users, spawn = compute_locust_targets(5000)      # 3×5000 = 15000
    assert users == 10000                            # clamped to users cap
    _, spawn2 = compute_locust_targets(2000)
    assert spawn2 == 1000                            # clamped to spawn cap


def test_locust_targets_custom_multiplier():
    assert compute_locust_targets(10, multiplier=5) == (50, 10)


# ---------------------------------------------------------------------------
# plan_moves
# ---------------------------------------------------------------------------


def _cust(stack_id, sections):
    return (uuid.uuid4(), stack_id, sections)


def test_plan_moves_single_stack_is_noop():
    a = uuid.uuid4()
    loads = [_cust(a, 5) for _ in range(5)]
    assert plan_moves([a], {a: uuid.uuid4()}, loads) == []


def test_plan_moves_balanced_is_noop():
    a, b = uuid.uuid4(), uuid.uuid4()
    srv = {a: uuid.uuid4(), b: uuid.uuid4()}
    loads = [_cust(a, 5), _cust(a, 5), _cust(b, 5), _cust(b, 5)]
    assert plan_moves([a, b], srv, loads) == []


def test_plan_moves_within_hysteresis_is_noop():
    a, b = uuid.uuid4(), uuid.uuid4()
    srv = {a: uuid.uuid4(), b: uuid.uuid4()}
    # A=11, B=9 → gap 2; threshold = max(2, ceil(.15*10)) = 2 → no move.
    loads = [_cust(a, 6), _cust(a, 5), _cust(b, 5), _cust(b, 4)]
    assert plan_moves([a, b], srv, loads) == []


def test_plan_moves_rebalances_drifted_agent():
    a, b = uuid.uuid4(), uuid.uuid4()
    sb = uuid.uuid4()
    srv = {a: uuid.uuid4(), b: sb}
    # A has 20 sections (4 customers × 5), B has none.
    loads = [_cust(a, 5) for _ in range(4)]
    moves = plan_moves([a, b], srv, loads)
    assert all(isinstance(m, PlannedMove) for m in moves)
    # Two customers move A→B, ending 10/10.
    assert len(moves) == 2
    assert all(m.from_stack_id == a and m.to_stack_id == b for m in moves)
    assert all(m.to_server_id == sb for m in moves)
    moved_sections = sum(m.sections for m in moves)
    assert moved_sections == 10  # A 20→10, B 0→10


def test_plan_moves_indivisible_big_customer_stays():
    a, b = uuid.uuid4(), uuid.uuid4()
    srv = {a: uuid.uuid4(), b: uuid.uuid4()}
    # A's whole load is one 10-section customer; moving it would just invert the
    # imbalance (B=10, A=0), so the balancer leaves it.
    loads = [(uuid.uuid4(), a, 10)]
    assert plan_moves([a, b], srv, loads) == []


def test_hysteresis_threshold_scales_with_size():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert _hysteresis_threshold({a: 5, b: 5}) == 2           # max(2, ceil(.15*5))
    assert _hysteresis_threshold({a: 100, b: 100}) == 15      # ceil(.15*100)


# ---------------------------------------------------------------------------
# render-time auto-scale
# ---------------------------------------------------------------------------


def _ctx(num_customers, *, autoscale, locust=None, multiplier=3, num_watch_only=0):
    rows = tuple(
        CustomerRow(
            section_name=f"s{i}",
            username=f"u{i}",
            password_plain="x",
            broker="ayandeh",
            isin=f"IRO{i:011d}",
            side=1,
        )
        for i in range(num_customers)
    ) + tuple(
        CustomerRow(
            section_name=f"w{i}",
            username=f"w{i}",
            password_plain="x",
            broker="ayandeh",
            isin=f"IRW{i:011d}",
            side=1,
            auto_sell_threshold=500,
            auto_sell_only=True,
        )
        for i in range(num_watch_only)
    )
    return StackRenderContext(
        agent_id=uuid.uuid4(),
        server_base_dir="/root/agents",
        agent_image_tag="img",
        ocr_service_url="http://ocr",
        customers=rows,
        autoscale_locust=autoscale,
        locust_users_multiplier=multiplier,
        locust=locust,
    )


def test_render_autoscale_on():
    import json

    out = json.loads(render_locust_config(_ctx(26, autoscale=True)))["locust"]
    assert out["users"] == 78 and out["spawn_rate"] == 26


def test_render_autoscale_off_uses_override():
    import json

    locust = LocustConfigRow(users=10, spawn_rate=10)
    out = json.loads(render_locust_config(_ctx(26, autoscale=False, locust=locust)))[
        "locust"
    ]
    assert out["users"] == 10 and out["spawn_rate"] == 10  # pre-feature behaviour


def test_render_autoscale_floor_from_manual_users():
    import json

    # Manual users=200 acts as a floor even though 3×5=15 is smaller.
    locust = LocustConfigRow(users=200, spawn_rate=10)
    out = json.loads(render_locust_config(_ctx(5, autoscale=True, locust=locust)))[
        "locust"
    ]
    assert out["users"] == 200 and out["spawn_rate"] == 5


def test_render_autoscale_excludes_auto_sell_only_sections():
    import json

    # Watch-only (auto_sell_only) sections get no locust user on the bot, so
    # the targets come from the 5 firing sections only: 15/5 (NOT 21/7).
    out = json.loads(
        render_locust_config(_ctx(5, autoscale=True, num_watch_only=2))
    )["locust"]
    assert out["users"] == 15 and out["spawn_rate"] == 5


# ---------------------------------------------------------------------------
# _load_customer_sections — the DB-side section count
# ---------------------------------------------------------------------------


async def test_load_customer_sections_excludes_auto_sell_only():
    """The count query must filter ``auto_sell_only`` rows out of the JOIN.

    If only the renderer excluded them, autobalance and render_locust_config
    would compute different users targets and fight on every reconcile — so
    this pins the SQL itself. The filter lives in the (outer) join condition,
    not the WHERE, so a customer whose only instructions are watch-only still
    appears with section count 0 (assigned, just weightless).
    """
    captured: dict[str, object] = {}

    result = MagicMock()
    result.all = MagicMock(return_value=[])

    async def _exec(stmt):
        captured["stmt"] = stmt
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_exec)

    assert await _load_customer_sections(db, uuid.uuid4()) == []

    sql = str(captured["stmt"])
    assert "auto_sell_only" in sql
    # Still an OUTER join — zero-section customers must not vanish.
    assert "LEFT OUTER JOIN" in sql
