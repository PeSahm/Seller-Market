"""Unit tests for the manual close-price service (``close_prices``).

set_close_price / clear_close_price upsert/delete a GLOBAL per-ISIN close price,
write one audit row (before/after), and are idempotent. No DB — a fake session
with get/add/delete/commit; get_close_prices uses a fake execute.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.audit import AuditLog
from app.models.instrument_close_price import InstrumentClosePrice
from app.services import close_prices as svc

_ISIN = "IRO1XXXX0001"


class _FakeDB:
    def __init__(self, row=None, insert_result=None):
        self._row = row
        self._insert_result = insert_result  # scalar returned by the if-absent INSERT
        self.added: list = []
        self.deleted: list = []
        self.commits = 0
        self.executed: list = []

    async def get(self, _model, _pk):
        return self._row

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1

    async def execute(self, stmt):
        self.executed.append(stmt)
        result = self._insert_result

        class _R:
            def scalar_one_or_none(self):
                return result

        return _R()


def _audits(db):
    return [a for a in db.added if isinstance(a, AuditLog)]


async def test_set_inserts_when_absent():
    actor = uuid.uuid4()
    db = _FakeDB(None)
    row = await svc.set_close_price(db, _ISIN, Decimal("7000"), "post-مجمع", actor)
    assert isinstance(row, InstrumentClosePrice)
    assert row.isin == _ISIN and row.close_price == Decimal("7000")
    assert row.note == "post-مجمع" and row.updated_by == actor
    aus = _audits(db)
    assert len(aus) == 1
    assert aus[0].action == "instrument_close_price.set"
    assert aus[0].target_type == "instrument_close_price"
    assert aus[0].target_id == _ISIN
    assert aus[0].before_json["close_price"] is None
    assert aus[0].after_json["close_price"] == "7000"
    assert db.commits == 1


async def test_set_updates_when_present():
    actor = uuid.uuid4()
    existing = InstrumentClosePrice(isin=_ISIN, close_price=Decimal("6000"), note="old")
    db = _FakeDB(existing)
    row = await svc.set_close_price(db, _ISIN, Decimal("7200"), None, actor)
    assert row is existing
    assert row.close_price == Decimal("7200") and row.note is None
    assert row.updated_by == actor
    aus = _audits(db)
    assert len(aus) == 1
    assert aus[0].before_json["close_price"] == "6000"
    assert aus[0].after_json["close_price"] == "7200"
    assert db.commits == 1


async def test_set_no_commit_defers_for_bulk():
    db = _FakeDB(None)
    await svc.set_close_price(
        db, _ISIN, Decimal("7000"), None, uuid.uuid4(), commit=False
    )
    assert db.commits == 0
    assert len(_audits(db)) == 1  # audit still staged


async def test_set_if_absent_inserts_and_audits():
    actor = uuid.uuid4()
    db = _FakeDB(insert_result=_ISIN)  # ON CONFLICT inserted → RETURNING isin
    out = await svc.set_close_price_if_absent(db, _ISIN, Decimal("7000"), "n", actor)
    assert out is True
    aus = _audits(db)
    assert len(aus) == 1 and aus[0].action == "instrument_close_price.set"
    assert aus[0].after_json["close_price"] == "7000"
    assert db.commits == 1


async def test_set_if_absent_skips_when_present():
    db = _FakeDB(insert_result=None)  # ON CONFLICT DO NOTHING → no row returned
    out = await svc.set_close_price_if_absent(
        db, _ISIN, Decimal("7000"), None, uuid.uuid4()
    )
    assert out is False
    assert _audits(db) == []  # no audit when nothing inserted
    assert db.commits == 1  # commit still fires (default)


async def test_set_if_absent_no_commit_defers_for_bulk():
    db = _FakeDB(insert_result=_ISIN)
    await svc.set_close_price_if_absent(
        db, _ISIN, Decimal("7000"), None, uuid.uuid4(), commit=False
    )
    assert db.commits == 0
    assert len(_audits(db)) == 1


async def test_clear_deletes_and_audits():
    existing = InstrumentClosePrice(isin=_ISIN, close_price=Decimal("7000"), note="n")
    db = _FakeDB(existing)
    out = await svc.clear_close_price(db, _ISIN, uuid.uuid4())
    assert out is True
    assert existing in db.deleted
    aus = _audits(db)
    assert len(aus) == 1 and aus[0].action == "instrument_close_price.clear"
    assert aus[0].before_json["close_price"] == "7000"
    assert aus[0].after_json["close_price"] is None
    assert db.commits == 1


async def test_clear_absent_is_noop():
    db = _FakeDB(None)
    out = await svc.clear_close_price(db, _ISIN, uuid.uuid4())
    assert out is False
    assert _audits(db) == []
    assert db.deleted == []
    assert db.commits == 0


async def test_get_close_price_returns_decimal():
    db = _FakeDB(InstrumentClosePrice(isin=_ISIN, close_price=Decimal("7000")))
    assert await svc.get_close_price(db, _ISIN) == Decimal("7000")


async def test_get_close_price_none_when_absent():
    assert await svc.get_close_price(_FakeDB(None), _ISIN) is None


async def test_get_close_prices_batch():
    class _Res:
        def all(self):
            return [(_ISIN, Decimal("7000")), ("IRO2YYYY0002", Decimal("5000"))]

    class _DB:
        def __init__(self):
            self.calls = 0

        async def execute(self, _stmt):
            self.calls += 1
            return _Res()

    db = _DB()
    out = await svc.get_close_prices(db, [_ISIN, "IRO2YYYY0002", ""])
    assert out == {_ISIN: Decimal("7000"), "IRO2YYYY0002": Decimal("5000")}
    assert db.calls == 1


async def test_get_close_prices_empty_input_makes_no_query():
    class _DB:
        async def execute(self, _stmt):  # pragma: no cover - must not run
            raise AssertionError("should not query for empty input")

    assert await svc.get_close_prices(_DB(), []) == {}
