"""Unit tests for the customer-wired render-context pipeline.

These tests cover :func:`app.services.stacks._build_render_context` against
a stack with mocked customer rows: we want to prove the DB rows are
projected into :class:`CustomerRow` instances and then handed to the
pure-function renderer such that each enabled customer becomes its own
``[section]`` block in the resulting ``config.ini``.

We deliberately avoid touching the DB or the Fernet layer. The DB ``execute``
return is faked with :class:`unittest.mock.MagicMock` shaped to look like a
SQLAlchemy ``Result``; the customer-service ``decrypt_password`` helper is
monkey-patched to return a known plaintext per row so the test asserts on
the rendered output, not on Fernet.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import stacks as stacks_svc
from app.services.rendering import render_config_ini


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _fake_customer(
    *,
    section_name: str | None = None,
    username: str = "u",
    password_enc: bytes = b"",
    broker: str = "bbi",
    isin: str = "IRO3AYHZ0001",
    side: int = 1,
    display_name: str = "Test",
    trade_instructions: "list[SimpleNamespace] | None" = None,
) -> SimpleNamespace:
    """Minimal Customer + its TradeInstructions stand-in.

    Post-migration 0003, the renderer's loader queries TradeInstruction
    separately, so each customer carries its associated TIs here.

    Two ways to use:

    * Default — pass ``section_name`` (+ optional isin/side), get ONE
      TradeInstruction attached. Convenience for single-trade tests
      that pre-date the split.
    * Multi — pass ``trade_instructions=[_fake_trade(...), ...]``
      directly. Used by the fanout test to exercise
      one-customer→many-sections rendering.
    """
    customer = SimpleNamespace(
        id=uuid.uuid4(),
        username=username,
        password_enc=password_enc,
        broker=broker,
        display_name=display_name,
    )
    if trade_instructions is not None:
        for ti in trade_instructions:
            ti.customer_id = customer.id
        customer.trade_instructions = trade_instructions
    else:
        assert section_name is not None, (
            "_fake_customer requires either section_name or trade_instructions"
        )
        customer.trade_instructions = [
            SimpleNamespace(
                id=uuid.uuid4(),
                customer_id=customer.id,
                isin=isin,
                side=side,
                section_name=section_name,
                comment=None,
            )
        ]
    return customer


def _fake_trade(
    *,
    section_name: str,
    isin: str = "IRO3AYHZ0001",
    side: int = 1,
) -> SimpleNamespace:
    """Single TradeInstruction stand-in for multi-TI tests."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        customer_id=None,  # set by _fake_customer
        isin=isin,
        side=side,
        section_name=section_name,
        comment=None,
    )


def _fake_stack(stack_id: uuid.UUID | None = None) -> SimpleNamespace:
    """Stack stand-in for ``_build_render_context``."""
    return SimpleNamespace(
        id=stack_id or uuid.uuid4(),
        server_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        stack_dir="/root/seller-market/agents/abc",
    )


def _fake_server() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        base_dir="/root/seller-market/agents",
    )


def _make_db(customer_rows: list[SimpleNamespace]) -> MagicMock:
    """Build a MagicMock that quacks like an ``AsyncSession`` for our needs.

    The render-context flow calls:

    * ``db.get(Server, stack.server_id)`` — return a fake server.
    * ``db.execute(<select Setting...>)`` — return a fake Result whose
      ``scalar_one_or_none`` returns ``None`` (so the defaults kick in).
    * ``db.execute(<select Customer...>)`` — return a fake Result whose
      ``scalars().all()`` returns ``customer_rows``.
    * ``db.execute(<select SchedulerJob...>)`` — Phase 5; return a fake
      Result with an empty ``scalars().all()`` so no jobs are projected.
    * ``db.execute(<select LocustConfig...>)`` — Phase 5; return a fake
      Result whose ``scalar_one_or_none`` returns ``None`` so the locust
      override falls back to fleet defaults.

    We can't easily distinguish the ``execute`` calls by inspecting the
    statement (it's a SQLAlchemy ``Select`` and pattern-matching against it
    would be brittle). Instead we use a side_effect that returns results in
    call order: two settings reads, the customer read, the scheduler-jobs
    read, then the locust-config read.
    """
    db = MagicMock()

    def _scalars_result(items):
        result = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=items)
        result.scalars = MagicMock(return_value=scalars_mock)
        return result

    settings_result = MagicMock()
    settings_result.scalar_one_or_none = MagicMock(return_value=None)

    customers_result = _scalars_result(customer_rows)

    # Post-0004 the loader has no enabled filter — every customer's TIs
    # are fetched in turn.
    ti_results = [
        _scalars_result(getattr(c, "trade_instructions", []))
        for c in customer_rows
    ]

    # Phase 5: empty scheduler-jobs list.
    scheduler_result = _scalars_result([])

    # Phase 5: no locust override.
    locust_result = MagicMock()
    locust_result.scalar_one_or_none = MagicMock(return_value=None)

    # Call order matches the body of ``_build_render_context`` →
    # ``_load_stack_customers``:
    # _read_setting × 2 → customers SELECT → per-customer TI SELECTs ×N →
    # list_jobs → get_locust_config → _read_setting × 2 (autoscale toggle +
    # multiplier; these don't affect config.ini, only locust).
    db.execute = AsyncMock(
        side_effect=[
            settings_result,  # agent_image_tag
            settings_result,  # ocr_service_url
            settings_result,  # bot_market_data_url (#110)
            customers_result,
            *ti_results,
            scheduler_result,
            locust_result,
            settings_result,  # enable_locust_autoscale
            settings_result,  # autobalance_users_multiplier
        ]
    )
    db.get = AsyncMock(return_value=_fake_server())
    return db


# ---------------------------------------------------------------------------
# 1. test_render_includes_one_section_per_customer
# ---------------------------------------------------------------------------


async def test_render_includes_one_section_per_customer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N customers in → N ``[section]`` blocks out.

    The render layer is pure, so the proof is straightforward: run the
    pipeline end-to-end with two customer rows and count ``[section_name]``
    headers in the output.
    """
    rows = [
        _fake_customer(section_name="a11111111_c22222222_bbi_IRO3AYHZ0001"),
        _fake_customer(section_name="a33333333_c44444444_mfd_IRO1FOLD0001"),
    ]
    db = _make_db(rows)
    stack = _fake_stack()

    async def _fake_decrypt(c):
        return "plain"

    monkeypatch.setattr(
        "app.services.customers.decrypt_password", _fake_decrypt
    )

    ctx = await stacks_svc._build_render_context(db, stack)
    out = render_config_ini(ctx)

    assert "[a11111111_c22222222_bbi_IRO3AYHZ0001]" in out
    assert "[a33333333_c44444444_mfd_IRO1FOLD0001]" in out
    # Exactly two customer sections — no [DEFAULT] header anymore (the original
    # trading bot doesn't expect one).
    assert out.count("\n[") + (1 if out.startswith("[") else 0) == 2
    assert "[DEFAULT]" not in out


# ---------------------------------------------------------------------------
# 2. test_section_header_format
# ---------------------------------------------------------------------------


async def test_section_header_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """Section header is exactly ``[a<8>_c<8>_<broker>_<isin>]``.

    The render layer takes ``section_name`` verbatim from the customer row,
    so this test pins the renderer's bracket-wrapping behaviour rather than
    the builder logic (the latter is covered in ``test_customers.py``).
    """
    section = "a4eebf408_c04cdabd0_bbi_IRO3AYHZ0001"
    rows = [_fake_customer(section_name=section)]
    db = _make_db(rows)
    stack = _fake_stack()

    async def _fake_decrypt(_c):
        return "p"

    monkeypatch.setattr(
        "app.services.customers.decrypt_password", _fake_decrypt
    )

    ctx = await stacks_svc._build_render_context(db, stack)
    out = render_config_ini(ctx)

    assert f"[{section}]" in out
    # And specifically the format: a<8 hex>_c<8 hex>_<broker>_<isin>
    import re

    assert re.search(
        r"^\[a[0-9a-f]{8}_c[0-9a-f]{8}_[a-z]+_IRO\w+\]$",
        f"[{section}]",
    )


# ---------------------------------------------------------------------------
# 3. test_render_passwords_are_decrypted_plaintext
# ---------------------------------------------------------------------------


async def test_render_passwords_are_decrypted_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``password =`` line carries the decrypted plaintext.

    Specifically NOT the Fernet ciphertext, NOT a base64 token, NOT the raw
    bytes — the deployed trading bot expects plaintext (that's the format
    the legacy ``config.ini`` already used) so we need to be sure we route
    through ``decrypt_password`` rather than just stringify
    ``customer.password_enc``.
    """
    row = _fake_customer(
        section_name="a00000000_c00000000_bbi_IRO3AYHZ0001",
        password_enc=b"ciphertext-bytes-not-the-plain",
    )
    db = _make_db([row])
    stack = _fake_stack()

    captured: list[object] = []

    async def _fake_decrypt(c):
        captured.append(c)
        return "MyPlaintext123!"

    monkeypatch.setattr(
        "app.services.customers.decrypt_password", _fake_decrypt
    )

    ctx = await stacks_svc._build_render_context(db, stack)
    out = render_config_ini(ctx)

    assert "password = MyPlaintext123!" in out
    # The ciphertext must not have leaked into the rendered output.
    assert "ciphertext-bytes-not-the-plain" not in out
    assert "b'" not in out  # No bytes repr leaked.
    # And decrypt_password was actually called (once per enabled row).
    assert len(captured) == 1
    assert captured[0] is row


# ---------------------------------------------------------------------------
# 5. test_render_with_persian_display_name_succeeds
# ---------------------------------------------------------------------------


async def test_render_with_persian_display_name_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row with a non-ASCII ``display_name`` doesn't break render.

    Only ``section_name`` (ASCII by construction) reaches the file, so the
    display name being Persian must be a no-op at the renderer boundary.
    This test guards against a future refactor that accidentally pipes
    ``display_name`` into the rendered output.
    """
    row = _fake_customer(
        section_name="a11111111_c22222222_bbi_IRO3AYHZ0001",
        display_name="حسین شفن",
    )
    db = _make_db([row])
    stack = _fake_stack()

    async def _fake_decrypt(_c):
        return "p"

    monkeypatch.setattr(
        "app.services.customers.decrypt_password", _fake_decrypt
    )

    ctx = await stacks_svc._build_render_context(db, stack)
    out = render_config_ini(ctx)

    # Render succeeded; ASCII section header present; Persian text NOT in file.
    assert "[a11111111_c22222222_bbi_IRO3AYHZ0001]" in out
    assert "حسین" not in out
    assert "شفن" not in out


# ---------------------------------------------------------------------------
# 6. test_render_deterministic_with_same_inputs
# ---------------------------------------------------------------------------


async def test_render_deterministic_with_same_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two calls with the same inputs produce byte-identical output.

    The renderer is pure (golden-file tested already in
    ``test_rendering.py``); this test pins the determinism of the
    end-to-end pipeline including the DB → projection layer in
    :func:`_build_render_context`. Important because the diff-preview UI
    diffs two renders against each other and any non-determinism would
    show up as a meaningless "diff" to the admin.
    """
    rows = [
        _fake_customer(section_name="a11111111_c22222222_bbi_IRO3AYHZ0001"),
        _fake_customer(section_name="a33333333_c44444444_mfd_IRO1FOLD0001"),
    ]

    async def _fake_decrypt(_c):
        return "p"

    monkeypatch.setattr(
        "app.services.customers.decrypt_password", _fake_decrypt
    )

    stack = _fake_stack()

    db1 = _make_db(list(rows))
    ctx1 = await stacks_svc._build_render_context(db1, stack)
    out1 = render_config_ini(ctx1)

    db2 = _make_db(list(rows))
    ctx2 = await stacks_svc._build_render_context(db2, stack)
    out2 = render_config_ini(ctx2)

    assert out1 == out2


# ---------------------------------------------------------------------------
# Cross-join fanout: one Customer → many [section] blocks (post-0003)
# ---------------------------------------------------------------------------


async def test_render_fanout_many_trades_per_customer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One Customer with N TradeInstructions produces N [section] blocks,
    each carrying the shared credentials.

    Also verifies the decrypt-once-per-customer property: the fake
    ``decrypt_password`` records its call args, and we assert it was
    called exactly once even though three sections were rendered.
    """
    customer = _fake_customer(
        username="shared-account",
        broker="ayandeh",
        trade_instructions=[
            _fake_trade(section_name="a1_c1_t1_ayandeh_IROAAA_s1", isin="IROAAA", side=1),
            _fake_trade(section_name="a1_c1_t2_ayandeh_IROBBB_s1", isin="IROBBB", side=1),
            _fake_trade(section_name="a1_c1_t3_ayandeh_IROCCC_s2", isin="IROCCC", side=2),
        ],
    )
    db = _make_db([customer])
    stack = _fake_stack()

    decrypt_calls: list = []

    async def _fake_decrypt(c):
        decrypt_calls.append(c)
        return "shared-plaintext"

    monkeypatch.setattr(
        "app.services.customers.decrypt_password", _fake_decrypt
    )

    ctx = await stacks_svc._build_render_context(db, stack)
    out = render_config_ini(ctx)

    # Three [section] blocks for the same customer.
    assert "[a1_c1_t1_ayandeh_IROAAA_s1]" in out
    assert "[a1_c1_t2_ayandeh_IROBBB_s1]" in out
    assert "[a1_c1_t3_ayandeh_IROCCC_s2]" in out

    # All three sections share the same credentials (one Customer).
    assert out.count("username = shared-account") == 3
    assert out.count("password = shared-plaintext") == 3
    assert out.count("broker = ayandeh") == 3

    # Decrypt was called ONCE per customer, NOT once per trade. The
    # plaintext is reused across all of that customer's instructions.
    assert len(decrypt_calls) == 1


# ---------------------------------------------------------------------------
# #110 auto-sell: auto_sell_threshold rendered only when set
# ---------------------------------------------------------------------------


def test_render_emits_auto_sell_threshold_only_when_set() -> None:
    """A section with ``auto_sell_threshold`` emits the line; otherwise omits it.

    Pure-renderer test (no DB): the bot reads this key from config.ini and
    arms the auto-sell monitor. Additive — sections without a threshold must
    render byte-identically to before.
    """
    from app.services.rendering import CustomerRow

    ctx = SimpleNamespace(
        customers=[
            CustomerRow("buy_armed", "u1", "p1", "ayandeh", "IRO1A", 1, auto_sell_threshold=500),
            CustomerRow("buy_unarmed", "u2", "p2", "ayandeh", "IRO1B", 1),
            CustomerRow("sell_section", "u3", "p3", "ayandeh", "IRO1C", 2, auto_sell_threshold=None),
        ]
    )
    out = render_config_ini(ctx)

    assert "auto_sell_threshold = 500" in out
    # Exactly one threshold line — only the armed buy section gets it.
    assert out.count("auto_sell_threshold") == 1
    # A 0 is falsy → treated as "no auto-sell" (omitted).
    ctx0 = SimpleNamespace(
        customers=[CustomerRow("z", "u", "p", "ayandeh", "IRO1Z", 1, auto_sell_threshold=0)]
    )
    assert "auto_sell_threshold" not in render_config_ini(ctx0)
