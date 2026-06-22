"""Unit tests for the per-server service-reachability monitor.

Covers the pure classifier, target/script builders, the per-server probe parse
(success + ssh-failure), the auth tier (target build, inline script, per-server
auth probe, and the deep-check per-account serialization invariant), and the
matrix shaper. All I/O (``run_command``, ``list_servers``, ``AsyncSessionLocal``,
the broker/customer queries, ``decrypt_password``) is stubbed — no live DB/SSH.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.services import service_monitor as svc

# --------------------------------------------------------------------------
# classify (pure)
# --------------------------------------------------------------------------


def test_classify_json_real_by_content_type():
    assert svc.classify("json", "200", "application/json", '{"x":1}') == "real"


def test_classify_json_real_by_auth_gate():
    # An API that answers 401/405 (no JSON ct) is still the real API.
    assert svc.classify("json", "401", "", "") == "real"
    assert svc.classify("json", "405", "text/plain", "") == "real"


def test_classify_json_placeholder_html_200():
    # The mdapi1 case: alive, but serving a plain HTML page where JSON is due.
    assert svc.classify("json", "200", "text/html; charset=utf-8", "<html>") == "placeholder"


def test_classify_json_real_by_body_marker():
    assert svc.classify("json", "200", "", "[{\"a\":1}]") == "real"


def test_classify_jpeg_real_and_placeholder():
    assert svc.classify("jpeg", "200", "image/jpeg", "\xff\xd8") == "real"
    assert svc.classify("jpeg", "200", "text/html", "<html>") == "placeholder"


def test_classify_json_isin_hit_miss():
    assert svc.classify("json_isin", "200", "application/json",
                        '[{"nc":"IRO1SROD0001","hap":1}]', isin="IRO1SROD0001") == "real"
    assert svc.classify("json_isin", "200", "application/json",
                        '[{"nc":"OTHER"}]', isin="IRO1SROD0001") == "degraded"
    assert svc.classify("json_isin", "200", "text/html", "<html>",
                        isin="IRO1SROD0001") == "placeholder"


def test_classify_transport_down():
    assert svc.classify("json", "000", "", "") == "down"
    assert svc.classify("json", "", "", "") == "down"


def test_classify_any_up():
    assert svc.classify("any", "404", "text/html", "x") == "up"
    assert svc.classify("any", "000", "", "") == "down"


# --------------------------------------------------------------------------
# build_targets
# --------------------------------------------------------------------------


class _Result:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        outer = self

        class _S:
            def all(self_inner):
                return list(outer._items)

            def first(self_inner):
                return outer._items[0] if outer._items else None

        return _S()


class _QueueDB:
    """Fake AsyncSession: each ``execute`` pops the next queued result."""

    def __init__(self, results):
        self._q = list(results)
        self.executed = []

    async def execute(self, stmt, *a, **k):
        self.executed.append(stmt)
        return self._q.pop(0) if self._q else _Result([])

    async def commit(self):
        pass


def _broker(code, family, label="", sort_order=0):
    return SimpleNamespace(code=code, family=family, label=label or code,
                           enabled=True, sort_order=sort_order)


async def test_build_targets_covers_brokers_pools_and_legacy(monkeypatch):
    brokers = [_broker("gs", "ephoenix", "Ghadir"),
               _broker("khobregan", "exir", "Khobregan"),
               _broker("ib", "ephoenix", "IBTrader")]
    db = _QueueDB([_Result(brokers)])

    async def fake_get_setting(_db, key):
        return {
            "ocr_service_url": "http://85.133.205.190:18080, http://host.docker.internal:18080",
            "bot_market_data_url": "http://5.10.248.55:8077",
        }[key]

    monkeypatch.setattr(svc.settings_store, "get_setting", fake_get_setting)

    targets = await svc.build_targets(db)
    keys = {t.key for t in targets}

    # Fixed shared + legacy + ibtrader + rlc.
    assert "ephoenix:marketdatagw" in keys
    assert "ephoenix-legacy:mdapi1" in keys
    assert {"ibtrader:identity", "ibtrader:api", "ibtrader:mdapi"} <= keys
    assert "rlc:core" in keys
    # Per ephoenix broker → identity + api; ib is NOT expanded per-broker.
    assert "ephoenix:identity:gs" in keys and "ephoenix:api:gs" in keys
    assert "ephoenix:identity:ib" not in keys
    # Exir tenant → captcha.
    assert "exir:khobregan" in keys
    # OCR pool split; host.docker.internal flagged host-local.
    ocr = {t.key: t for t in targets if t.group == "ocr"}
    assert any(t.host_local for t in ocr.values())
    assert any(not t.host_local for t in ocr.values())
    # Market-data sidecar.
    assert any(t.group == "market-data" for t in targets)
    # The rlc target carries the probe isin and an encoded url (no raw quotes).
    rlc = next(t for t in targets if t.key == "rlc:core")
    assert rlc.isin == svc.PROBE_ISIN and "'" not in rlc.url


# --------------------------------------------------------------------------
# build_probe_script
# --------------------------------------------------------------------------


def test_build_probe_script_shape():
    targets = [
        svc.Target("rlc:core", "rlc", "RLC", svc._rlc_url("IRO1SROD0001"),
                   "json_isin", isin="IRO1SROD0001"),
        svc.Target("a:b", "ephoenix", "A", "https://a/b", "json"),
    ]
    script = svc.build_probe_script(targets, curl_timeout=5)
    assert "--noproxy" in script
    assert "unset http_proxy" in script
    assert script.count("probe ") >= 2  # one probe invocation per target + the fn def
    # The single-quoted RLC url is shlex-quoted so it survives the shell.
    assert "getstockprice2" in script
    assert "-m 5" in script


# --------------------------------------------------------------------------
# probe_server
# --------------------------------------------------------------------------


def _server():
    return SimpleNamespace(id=uuid.uuid4(), name="tebyan2", host="185.232.152.177")


async def test_probe_server_classifies_and_skips_host_local(monkeypatch):
    targets = [
        svc.Target("a", "ephoenix", "A", "https://a", "json"),
        svc.Target("b", "ephoenix", "B", "https://b", "json"),
        svc.Target("hl", "ocr", "HL", "http://host.docker.internal:18080/",
                   "any", host_local=True),
    ]
    stdout = (
        "a\x1f200|application/json|0.10\x1f{\"ok\":1}\n"
        "b\x1f200|text/html; charset=utf-8|0.20\x1f<html>\n"
    )

    async def fake_run(server, cmd, **kw):
        return SimpleNamespace(stdout=stdout, stderr="", exit_code=0)

    monkeypatch.setattr(svc, "run_command", fake_run)

    out = await svc.probe_server(_server(), targets)
    by = {r["target_key"]: r for r in out}
    assert by["a"]["state"] == "real"
    assert by["a"]["latency_ms"] == 100
    assert by["b"]["state"] == "placeholder"
    assert by["hl"]["state"] == "skipped"
    assert by["_meta:__ssh__"]["state"] == "up"


async def test_probe_server_ssh_failure_all_down(monkeypatch):
    targets = [
        svc.Target("a", "ephoenix", "A", "https://a", "json"),
        svc.Target("hl", "ocr", "HL", "http://host.docker.internal:18080/",
                   "any", host_local=True),
    ]

    async def boom(server, cmd, **kw):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(svc, "run_command", boom)

    out = await svc.probe_server(_server(), targets)
    by = {r["target_key"]: r for r in out}
    assert by["a"]["state"] == "down"
    assert by["hl"]["state"] == "skipped"  # host-local always skipped, even on ssh fail
    assert by["_meta:__ssh__"]["state"] == "down"


# --------------------------------------------------------------------------
# auth tier
# --------------------------------------------------------------------------


def test_build_auth_probe_script_is_safe_and_reuses_bot_code():
    s = svc.build_auth_probe_script("ephoenix")
    assert "json.load(sys.stdin)" in s
    assert "decode_captcha" in s
    assert "EphoenixAPIClient" in s
    assert "get_adapter" in s  # exir branch
    # The inline script swallows everything → never crashes the exec.
    assert "except Exception" in s


async def test_build_auth_targets_dedups_and_decrypts(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4(), username="Mostafa")
    cust1 = SimpleNamespace(id=uuid.uuid4(), broker="ayandeh", username="4580090306",
                            agent_id=agent.id)
    cust2 = SimpleNamespace(id=uuid.uuid4(), broker="ayandeh", username="4580090306",
                            agent_id=agent.id)  # dup (broker, username)
    cust3 = SimpleNamespace(id=uuid.uuid4(), broker="khobregan", username="4580090306",
                            agent_id=agent.id)
    brokers = [_broker("ayandeh", "ephoenix", "Ayandeh"),
               _broker("khobregan", "exir", "Khobregan")]
    # execute order: User, Customer, Broker
    db = _QueueDB([_Result([agent]), _Result([cust1, cust2, cust3]), _Result(brokers)])

    monkeypatch.setattr(svc, "get_settings",
                        lambda: SimpleNamespace(monitor_probe_agent_username="Mostafa"))

    async def fake_decrypt(c):
        return "pw-" + c.broker

    monkeypatch.setattr(svc.services_customers, "decrypt_password", fake_decrypt)

    targets = await svc.build_auth_targets(db)
    by = {t.code: t for t in targets}
    assert set(by) == {"ayandeh", "khobregan"}  # dedup collapsed the two ayandeh
    assert by["ayandeh"].family == "ephoenix"
    assert by["khobregan"].family == "exir"
    assert by["ayandeh"].password == "pw-ayandeh"


async def test_probe_server_auth_success_failure_and_no_bot(monkeypatch):
    acct = svc.AuthTarget(code="ayandeh", family="ephoenix", label="Ayandeh",
                          username="acct-user", password="s3cr3t-PW-zzz",
                          isin="IRO1SROD0001")
    calls = []

    async def fake_run(server, cmd, *, stdin_data=None, timeout=30.0, **kw):
        calls.append({"cmd": cmd, "stdin": stdin_data})
        if "docker ps" in cmd:
            return SimpleNamespace(stdout="sm-agent-abc-bot\n", stderr="", exit_code=0)
        # docker exec → the inline probe printed its json line.
        return SimpleNamespace(
            stdout='{"ok": true, "detail": "login+marketdata ok سرود", "latency_ms": 1234}\n',
            stderr="", exit_code=0,
        )

    monkeypatch.setattr(svc, "run_command", fake_run)
    res = await svc.probe_server_auth(_server(), acct)
    assert res["state"] == "real"
    # Account-specific key (broker + username) so two accounts on one broker
    # don't collide on the (server_id, target_key) upsert.
    assert res["target_key"] == "auth:ayandeh:acct-user"
    assert res["group_name"] == "auth-ephoenix"
    # Creds went via stdin, NEVER on the command line.
    exec_call = [c for c in calls if "docker exec" in c["cmd"]][0]
    assert exec_call["stdin"] is not None
    assert acct.password.encode() in exec_call["stdin"]
    assert acct.password not in exec_call["cmd"]
    assert "login+marketdata" in (res["detail"] or "")

    # Failure JSON → down.
    async def fake_run_fail(server, cmd, *, stdin_data=None, timeout=30.0, **kw):
        if "docker ps" in cmd:
            return SimpleNamespace(stdout="sm-agent-abc-bot\n", stderr="", exit_code=0)
        return SimpleNamespace(stdout='{"ok": false, "detail": "bad captcha"}\n',
                               stderr="", exit_code=0)

    monkeypatch.setattr(svc, "run_command", fake_run_fail)
    res2 = await svc.probe_server_auth(_server(), acct)
    assert res2["state"] == "down" and "bad captcha" in res2["detail"]

    # No bot container on the host → skipped.
    async def fake_run_nobot(server, cmd, *, stdin_data=None, timeout=30.0, **kw):
        return SimpleNamespace(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(svc, "run_command", fake_run_nobot)
    res3 = await svc.probe_server_auth(_server(), acct)
    assert res3["state"] == "skipped"


async def test_deep_check_serializes_per_account(monkeypatch):
    """The deep-check must never log in to the same account from two servers at
    once — within an account, servers are probed sequentially."""
    accts = [
        svc.AuthTarget("ayandeh", "ephoenix", "Ayandeh", "u", "p", "IRO1SROD0001"),
        svc.AuthTarget("khobregan", "exir", "Khobregan", "u", "p", "IRO1SROD0001"),
    ]
    servers = [_server(), _server(), _server()]

    async def fake_build_auth_targets(db):
        return accts

    async def fake_list_servers(db):
        return servers

    inflight: dict[str, int] = {}
    peak: dict[str, int] = {}

    async def fake_probe(server, acct, **kw):
        inflight[acct.code] = inflight.get(acct.code, 0) + 1
        peak[acct.code] = max(peak.get(acct.code, 0), inflight[acct.code])
        await asyncio.sleep(0.005)
        inflight[acct.code] -= 1
        return {"target_key": f"auth:{acct.code}", "group_name": f"auth-{acct.family}",
                "name": acct.label, "url": "", "state": "real",
                "http_status": None, "content_type": None, "latency_ms": 1,
                "detail": "ok"}

    recorded = []

    async def fake_record(s, server_id, results):
        recorded.append((server_id, results))

    class _FakeSession:
        def __init__(self):
            self.added = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def add(self, o):
            self.added.append(o)

        async def commit(self):
            pass

    sessions = []

    def _session_factory():
        s = _FakeSession()
        sessions.append(s)
        return s

    monkeypatch.setattr(svc, "build_auth_targets", fake_build_auth_targets)
    monkeypatch.setattr(svc, "list_servers", fake_list_servers)
    monkeypatch.setattr(svc, "probe_server_auth", fake_probe)
    monkeypatch.setattr(svc, "record_results", fake_record)
    monkeypatch.setattr(svc, "AsyncSessionLocal", _session_factory)

    n = await svc.deep_check_once(uuid.uuid4(), concurrency=3)
    assert n == 2
    # Per-account peak concurrency is exactly 1 → no same-account multi-IP login.
    assert peak == {"ayandeh": 1, "khobregan": 1}
    # Every (account, server) was recorded.
    assert len(recorded) == len(accts) * len(servers)
    # An audit row was written.
    assert any(s.added for s in sessions)


async def test_deep_check_single_flight_skips_concurrent_run(monkeypatch):
    """A second deep-check launched while one is in flight is skipped, so two
    runs can't log the same account in from two IPs at once."""
    lock = svc._get_deep_check_lock()
    await lock.acquire()
    try:
        n = await svc.deep_check_once(uuid.uuid4())
        assert n == 0  # skipped — a run already holds the lock
    finally:
        lock.release()


async def test_record_results_prune_deletes_stale_only_when_asked():
    class _CountDB:
        def __init__(self):
            self.executes = 0
            self.commits = 0

        async def execute(self, stmt, *a, **k):
            self.executes += 1
            return None

        async def commit(self):
            self.commits += 1

    res = [{"target_key": "a", "group_name": "ephoenix", "name": "A",
            "url": "u", "state": "real"}]
    db1 = _CountDB()
    await svc.record_results(db1, uuid.uuid4(), res)
    assert db1.executes == 1  # upsert only

    db2 = _CountDB()
    await svc.record_results(db2, uuid.uuid4(), res, prune_others=True)
    assert db2.executes == 2  # upsert + prune-delete


# --------------------------------------------------------------------------
# build_service_matrix
# --------------------------------------------------------------------------


def _row(server_id, key, group, name, state, **kw):
    return SimpleNamespace(
        server_id=server_id, target_key=key, group_name=group, name=name,
        url=kw.get("url", "https://x"), state=state,
        http_status=kw.get("http_status"), content_type=kw.get("content_type"),
        latency_ms=kw.get("latency_ms"), detail=kw.get("detail"),
        probed_at=kw.get("probed_at"),
    )


async def test_build_service_matrix_groups_and_cells(monkeypatch):
    s1 = SimpleNamespace(id=uuid.uuid4(), name="alpha", host="1.1.1.1")
    s2 = SimpleNamespace(id=uuid.uuid4(), name="beta", host="2.2.2.2")
    rows = [
        _row(s1.id, "ephoenix:marketdatagw", "ephoenix", "marketdatagw", "real"),
        _row(s2.id, "ephoenix:marketdatagw", "ephoenix", "marketdatagw", "down"),
        _row(s1.id, "rlc:core", "rlc", "RLC", "real"),
        _row(s1.id, "_meta:__ssh__", "_meta", "ssh", "up"),
        _row(s2.id, "_meta:__ssh__", "_meta", "ssh", "down"),
    ]

    class _RowsDB:
        async def execute(self, *a, **k):
            return _Result(rows)

    async def fake_list_servers(db):
        return [s1, s2]

    monkeypatch.setattr(svc, "list_servers", fake_list_servers)

    m = await svc.build_service_matrix(_RowsDB())
    assert m["server_count"] == 2
    # _meta excluded from the body rows but surfaced as the column ssh badge.
    group_keys = {g["group"] for g in m["groups"]}
    assert "_meta" not in group_keys
    assert "ephoenix" in group_keys and "rlc" in group_keys
    cols = {c["name"]: c for c in m["servers"]}
    assert cols["alpha"]["ssh_state"] == "up"
    assert cols["beta"]["ssh_state"] == "down"
    # The marketdatagw row has a real cell on s1, a down cell on s2.
    eph = next(g for g in m["groups"] if g["group"] == "ephoenix")
    mdgw = next(r for r in eph["rows"] if r["key"] == "ephoenix:marketdatagw")
    assert mdgw["cells"][s1.id].state == "real"
    assert mdgw["cells"][s2.id].state == "down"
    assert m["down"] == 1  # only the s2 marketdatagw row (meta excluded)
