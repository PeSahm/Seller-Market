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
    section_name: str,
    username: str = "u",
    password_enc: bytes = b"",
    broker: str = "bbi",
    isin: str = "IRO3AYHZ0001",
    side: int = 1,
    enabled: bool = True,
    display_name: str = "Test",
) -> SimpleNamespace:
    """Minimal Customer + one matching TradeInstruction stand-in.

    Post-migration 0003, the renderer's loader queries TradeInstruction
    separately, so each customer carries its associated TI (or list of
    TIs) here. ``section_name``/``isin``/``side`` describe the
    TradeInstruction that the (Customer × TI) row produces.
    """
    customer = SimpleNamespace(
        id=uuid.uuid4(),
        username=username,
        password_enc=password_enc,
        broker=broker,
        enabled=enabled,
        display_name=display_name,
    )
    customer.trade_instructions = [
        SimpleNamespace(
            id=uuid.uuid4(),
            customer_id=customer.id,
            isin=isin,
            side=side,
            section_name=section_name,
            enabled=enabled,
            comment=None,
        )
    ]
    return customer


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

    # One trade_instructions result per ENABLED customer — the loader's
    # ``if not c.enabled: continue`` guard skips the TI query for
    # disabled customers, so feeding a result for them would shift the
    # AsyncMock side_effect sequence.
    ti_results = [
        _scalars_result(getattr(c, "trade_instructions", []))
        for c in customer_rows
        if c.enabled
    ]

    # Phase 5: empty scheduler-jobs list.
    scheduler_result = _scalars_result([])

    # Phase 5: no locust override.
    locust_result = MagicMock()
    locust_result.scalar_one_or_none = MagicMock(return_value=None)

    # Call order matches the body of ``_build_render_context`` →
    # ``_load_stack_customers``:
    # _read_setting × 2 → customers SELECT → per-customer TI SELECTs ×N →
    # list_jobs → get_locust_config.
    db.execute = AsyncMock(
        side_effect=[
            settings_result,
            settings_result,
            customers_result,
            *ti_results,
            scheduler_result,
            locust_result,
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
# 3. test_render_omits_disabled_customers
# ---------------------------------------------------------------------------


async def test_render_omits_disabled_customers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled rows must NOT appear in the rendered output.

    The SELECT in :func:`_load_stack_customers` already filters on
    ``enabled = TRUE``, but we additionally guard inside the projection loop.
    This test exercises the projection-loop guard by handing back a row that
    the SELECT (here a mock) didn't filter out — proving the secondary guard
    is load-bearing.
    """
    enabled_row = _fake_customer(
        section_name="a11111111_c22222222_bbi_IRO3AYHZ0001", enabled=True
    )
    disabled_row = _fake_customer(
        section_name="a99999999_c99999999_bbi_IRO9DISABLE9", enabled=False
    )
    db = _make_db([enabled_row, disabled_row])
    stack = _fake_stack()

    async def _fake_decrypt(_c):
        return "p"

    monkeypatch.setattr(
        "app.services.customers.decrypt_password", _fake_decrypt
    )

    ctx = await stacks_svc._build_render_context(db, stack)
    out = render_config_ini(ctx)

    assert "[a11111111_c22222222_bbi_IRO3AYHZ0001]" in out
    assert "IRO9DISABLE9" not in out
    assert "[a99999999_c99999999_bbi_IRO9DISABLE9]" not in out


# ---------------------------------------------------------------------------
# 4. test_render_passwords_are_decrypted_plaintext
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
