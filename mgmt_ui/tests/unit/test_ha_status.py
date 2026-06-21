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


class _FakeDB:
    """Returns the queued results for the 3 rollup queries (servers, stacks,
    alerts) in order."""

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
    monkeypatch.setattr(
        ha_status.settings_store, "get_setting", AsyncMock(return_value="http://o:18080")
    )

    db = _FakeDB(
        [
            _FakeResult([("online", 3), ("offline", 1)]),  # servers
            _FakeResult([("up", 10), ("down", 2)]),        # stacks
            _FakeResult([("critical", 1), ("error", 2), ("warning", 5)]),  # alerts
        ]
    )

    snap = await ha_status.build_ha_status(db, is_worker_leader=False)

    assert snap["main_db"]["reachable"] is True
    assert snap["spare_db"]["host"] == "spare"
    assert snap["ocr"][0]["reachable"] is True
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
    monkeypatch.setattr(
        ha_status.settings_store,
        "get_setting",
        AsyncMock(side_effect=RuntimeError("db down")),
    )

    db = _FakeDB([_FakeResult([]), _FakeResult([]), _FakeResult([])])
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
    monkeypatch.setattr(
        ha_status.settings_store, "get_setting", AsyncMock(return_value="http://o:18080")
    )

    db = _FakeDB([_FakeResult([]), _FakeResult([]), _FakeResult([])])
    snap = await ha_status.build_ha_status(db)

    assert snap["active_db"] == "main"
    assert snap["on_spare"] is False
    assert snap["backups"]["count"] == 0  # no manifest on disk in tests
    assert snap["backups"]["retention"] == 4  # the operator's "keep 4"
