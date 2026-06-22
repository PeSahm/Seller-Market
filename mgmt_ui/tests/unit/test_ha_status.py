"""Tests for the consolidated HA status snapshot (#156 WS4).

The network/DB probes are mocked so the assembly logic (rollups, totals,
graceful structure) is exercised offline.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services import ha_status

# --- _dsn_host_port (pure) -------------------------------------------------


@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("postgresql+asyncpg://u:p@db.example:65444/mgmt_ui", ("db.example", 65444)),
        ("postgresql://u:p@10.0.0.5/mgmt", ("10.0.0.5", None)),
        ("postgresql+psycopg2://u:p@host/db", ("host", None)),
        ("not a url", ("?", None)),
    ],
)
def test_dsn_host_port(dsn, expected):
    assert ha_status._dsn_host_port(dsn) == expected


# --- _probe_ocr host-local labelling (offline-safe) ------------------------


@pytest.mark.asyncio
async def test_probe_ocr_labels_host_local_without_network():
    # host.docker.internal is never probed (mgmt can't resolve it) -> labelled,
    # no httpx .get call, so this is safe with no network.
    out = await ha_status._probe_ocr(["http://host.docker.internal:18080"])
    assert out == [
        {
            "url": "http://host.docker.internal:18080",
            "host_local": True,
            "reachable": None,
            "latency_ms": None,
        }
    ]


# --- build_ha_status assembly ---------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):  # for the enabled-brokers query
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Returns the queued results in order: the enabled-brokers query first (via
    ``.scalars().all()``), then the 3 rollup queries (servers, stacks, alerts)."""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, *a, **k):
        return self._results.pop(0)


def _ok(host, port, ms):
    return {"host": host, "port": port, "reachable": True, "latency_ms": ms}


@pytest.mark.asyncio
async def test_build_ha_status_assembles_snapshot(monkeypatch):
    monkeypatch.setattr(ha_status, "_probe_main_db", AsyncMock(return_value=_ok("win", 65444, 2.2)))
    monkeypatch.setattr(
        ha_status, "_probe_spare_db", AsyncMock(return_value=_ok("spare", 5432, 1.0))
    )
    ocr_row = {"url": "http://o:18080", "host_local": False, "reachable": True, "latency_ms": 5.0}
    monkeypatch.setattr(ha_status, "_probe_ocr", AsyncMock(return_value=[ocr_row]))
    ext_row = {"group": "ephoenix", "name": "market-data (marketdatagw)",
               "url": "https://marketdatagw.ephoenix.ir/", "reachable": True,
               "status": 200, "latency_ms": 80.0}
    monkeypatch.setattr(ha_status, "_probe_external", AsyncMock(return_value=[ext_row]))
    monkeypatch.setattr(
        ha_status.settings_store, "get_setting", AsyncMock(return_value="http://o:18080")
    )

    db = _FakeDB(
        [
            _FakeResult([]),                               # enabled brokers
            _FakeResult([("online", 3), ("offline", 1)]),  # servers
            _FakeResult([("up", 10), ("down", 2)]),        # stacks
            _FakeResult([("critical", 1), ("error", 2), ("warning", 5)]),  # alerts
        ]
    )

    snap = await ha_status.build_ha_status(db, is_worker_leader=False)

    assert snap["main_db"]["reachable"] is True
    assert snap["spare_db"]["host"] == "spare"
    assert snap["ocr"][0]["reachable"] is True
    assert snap["external"][0]["reachable"] is True
    assert snap["external_down"] == 0
    assert snap["servers"] == {"online": 3, "offline": 1}
    assert snap["servers_total"] == 4
    assert snap["stacks_total"] == 12
    assert snap["alerts_attention"] == 3  # critical + error
    assert snap["is_worker_leader"] is False


@pytest.mark.asyncio
async def test_build_ha_status_graceful_when_setting_errors(monkeypatch):
    """A failing settings lookup must not break the page — it falls back to the
    default OCR URL."""
    monkeypatch.setattr(ha_status, "_probe_main_db", AsyncMock(return_value=_ok("win", 65444, 2.2)))
    monkeypatch.setattr(ha_status, "_probe_spare_db", AsyncMock(return_value=None))
    captured = {}

    async def _fake_ocr(urls):
        captured["urls"] = urls
        return []

    monkeypatch.setattr(ha_status, "_probe_ocr", _fake_ocr)
    monkeypatch.setattr(ha_status, "_probe_external", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ha_status.settings_store,
        "get_setting",
        AsyncMock(side_effect=RuntimeError("db down")),
    )

    db = _FakeDB([_FakeResult([]), _FakeResult([]), _FakeResult([]), _FakeResult([])])
    snap = await ha_status.build_ha_status(db)

    assert snap["spare_db"] is None
    assert snap["recovery_configured"] is False
    # fell back to the default OCR url (non-empty)
    assert captured["urls"]


@pytest.mark.asyncio
async def test_build_ha_status_includes_active_db_and_backups(monkeypatch):
    from app import db as db_mod

    db_mod._reset_to_main_for_tests()
    monkeypatch.setattr(ha_status, "_probe_main_db", AsyncMock(return_value=_ok("win", 65444, 2.2)))
    monkeypatch.setattr(ha_status, "_probe_spare_db", AsyncMock(return_value=None))
    monkeypatch.setattr(ha_status, "_probe_ocr", AsyncMock(return_value=[]))
    monkeypatch.setattr(ha_status, "_probe_external", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ha_status.settings_store, "get_setting", AsyncMock(return_value="http://o:18080")
    )

    db = _FakeDB([_FakeResult([]), _FakeResult([]), _FakeResult([]), _FakeResult([])])
    snap = await ha_status.build_ha_status(db)

    assert snap["active_db"] == "main"
    assert snap["on_spare"] is False
    assert snap["backups"]["count"] == 0  # no manifest on disk in tests
    assert snap["backups"]["retention"] == 4  # the operator's "keep 4"


# --- external-service probe targets ---------------------------------------


def _broker(code, family, label=None):
    from types import SimpleNamespace
    return SimpleNamespace(code=code, family=family, label=label)


def test_ext_targets_builds_service_list():
    targets = ha_status._ext_targets([
        _broker("ayandeh", "ephoenix", "Ayandeh"),
        _broker("ib", "ephoenix", "IB"),            # on ibtrader.ir (fixed probes), not api-ib.ephoenix
        _broker("khobregan", "exir", "Khobregan"),
    ])
    urls = [t["url"] for t in targets]
    assert "https://marketdatagw.ephoenix.ir/" in urls   # the host that just changed
    assert "https://api.ibtrader.ir/" in urls
    assert "https://mdapi.ibtrader.ir/" in urls
    assert "https://core.tadbirrlc.com/" in urls
    assert "https://api-ayandeh.ephoenix.ir/" in urls    # per-broker ephoenix
    assert "https://khobregan.exirbroker.com/" in urls   # exir tenant
    assert "https://api-ib.ephoenix.ir/" not in urls     # ib not double-probed
    assert {t["group"] for t in targets} == {"ephoenix", "ibtrader", "exir", "rlc"}


@pytest.mark.asyncio
async def test_probe_external_empty_makes_no_request():
    assert await ha_status._probe_external([]) == []
