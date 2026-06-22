"""Per-server service reachability monitor (the ``/admin/server-services`` matrix).

Two tiers, both probing FROM each managed server (so reachability reflects what
that server's bots actually experience — the ``marketdatagw`` lesson: reachable
from Tebyan, not from PouyanIt):

  * **Unauthenticated** (frequent, leader-gated worker): for every server, curl
    every endpoint we depend on (OCR, ephoenix per broker + the shared
    ``marketdatagw`` + the legacy ``mdapi1``, ibtrader, exir tenants, RLC, the
    market-data sidecar) with the proxy bypassed, and classify a genuine API
    apart from a live-but-placeholder host (HTML where JSON is expected).

  * **Authenticated** (manual "Deep check" only): a real login + a real API call
    using Mostafa's credential, run INSIDE that host's bot container
    (``docker exec -i <bot> python -c <script>``, creds on stdin), reusing the
    bot image's broker code — the truest "this broker actually trades from this
    server" signal. Serialized per ``(broker, account)`` so the same account is
    never logged in from two server IPs at once.

Results upsert into ``service_probe_results`` (one row per ``(server, target)``);
``build_service_matrix`` shapes them into the endpoint x server grid.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import shlex
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.audit import AuditLog
from app.models.brokers import Broker
from app.models.customers import Customer
from app.models.service_probe import ServiceProbeResult
from app.models.users import User
from app.services import customers as services_customers
from app.services import settings_store
from app.services.broker_client import _ocr_base_urls
from app.services.servers import list_servers
from app.services.ssh.commands import run_command
from app.settings import get_settings

logger = logging.getLogger(__name__)

# A liquid instrument present on both ephoenix (سرود) and reachable via RLC — used
# as the harmless probe ISIN for the market-data + RLC + exir-prepare checks.
PROBE_ISIN = "IRO1SROD0001"

# Display order + labels for the matrix groups (``_meta`` is never shown).
GROUP_ORDER: list[tuple[str, str]] = [
    ("ocr", "OCR"),
    ("market-data", "Market-data sidecar"),
    ("ephoenix", "ephoenix"),
    ("ephoenix-legacy", "ephoenix (legacy)"),
    ("ibtrader", "ibtrader"),
    ("exir", "Exir"),
    ("rlc", "RLC (tadbir)"),
    ("auth-ephoenix", "Authenticated · ephoenix (real login)"),
    ("auth-exir", "Authenticated · Exir (real login)"),
]

_SSH_META_KEY = "_meta:__ssh__"
# Groups whose rows are NOT refreshed by the unauthenticated tick (so the tick's
# stale-row prune must never delete them).
_AUTH_GROUPS = ("auth-ephoenix", "auth-exir")
# A single transient SSH blip ("Channel closed" right after a container restart /
# a cold connection pool) must NOT paint a whole healthy server "down". So the
# per-server probe retries once after a short pause before declaring all-down —
# enough for the pool to evict the stale transport and reconnect.
_SSH_RETRY_DELAY = 1.5

# Single-flight guard for the authenticated deep-check: two concurrent runs (e.g.
# two button clicks) would defeat the per-(broker, account) serialization and log
# the same account in from two server IPs at once. Lazily created per running
# loop so it never trips the "bound to a different event loop" footgun in tests.
_deep_check_lock: Optional[asyncio.Lock] = None
_deep_check_lock_loop = None


def _get_deep_check_lock() -> asyncio.Lock:
    global _deep_check_lock, _deep_check_lock_loop
    loop = asyncio.get_running_loop()
    if _deep_check_lock is None or _deep_check_lock_loop is not loop:
        _deep_check_lock = asyncio.Lock()
        _deep_check_lock_loop = loop
    return _deep_check_lock


@dataclass
class Target:
    """One unauthenticated probe target (shared across all servers)."""

    key: str
    group: str
    name: str
    url: str
    expect: str  # json | jpeg | json_isin | any
    isin: Optional[str] = None
    host_local: bool = False  # host.docker.internal — can't be probed remotely


@dataclass
class AuthTarget:
    """One authenticated probe account (Mostafa's credential for a broker)."""

    code: str
    family: str
    label: str
    username: str
    password: str
    isin: str


# ---------------------------------------------------------------------------
# Unauthenticated tier
# ---------------------------------------------------------------------------


def _rlc_url(isin: str) -> str:
    """The public RLC StockInformationHandler URL (mirrors rlc_price._build_url)."""
    blob = "{'Type':'getstockprice2','la':'Fa','arr':'" + isin + "'}"
    return (
        "https://core.tadbirrlc.com//StockInformationHandler?"
        + urllib.parse.quote(blob)
        + "&jsoncallback="
    )


async def build_targets(db: AsyncSession) -> list[Target]:
    """Assemble the shared unauthenticated probe list (same for every server)."""
    targets: list[Target] = [
        Target(
            "ephoenix:marketdatagw", "ephoenix", "market-data (marketdatagw)",
            "https://marketdatagw.ephoenix.ir/api/v2/instruments/full", "json",
        ),
        Target(
            "ephoenix-legacy:mdapi1", "ephoenix-legacy", "market-data (legacy mdapi1)",
            "https://mdapi1.ephoenix.ir/api/v2/instruments/full", "json",
        ),
        Target(
            "ibtrader:identity", "ibtrader", "identity (captcha)",
            "https://identity.ibtrader.ir/api/Captcha/GetCaptcha", "json",
        ),
        Target(
            "ibtrader:api", "ibtrader", "api (orders)",
            "https://api.ibtrader.ir/api/v2/orders/GetOrders", "json",
        ),
        Target(
            "ibtrader:mdapi", "ibtrader", "mdapi (market-data)",
            "https://mdapi.ibtrader.ir/api/v2/instruments/full", "json",
        ),
        Target(
            "rlc:core", "rlc", "tadbir StockInformationHandler",
            _rlc_url(PROBE_ISIN), "json_isin", isin=PROBE_ISIN,
        ),
    ]

    try:
        brokers = list(
            (
                await db.execute(
                    select(Broker)
                    .where(Broker.enabled.is_(True))
                    .order_by(Broker.family, Broker.sort_order)
                )
            ).scalars().all()
        )
    except Exception:  # noqa: BLE001 — display-only, never 500
        brokers = []
    for b in brokers:
        if b.family == "exir":
            targets.append(Target(
                f"exir:{b.code}", "exir", b.label or b.code,
                f"https://{b.code}.exirbroker.com/captcha", "jpeg",
            ))
        elif b.family == "ephoenix" and b.code != "ib":
            label = b.label or b.code
            targets.append(Target(
                f"ephoenix:identity:{b.code}", "ephoenix", f"{label} · identity",
                f"https://identity-{b.code}.ephoenix.ir/api/Captcha/GetCaptcha", "json",
            ))
            targets.append(Target(
                f"ephoenix:api:{b.code}", "ephoenix", f"{label} · api",
                f"https://api-{b.code}.ephoenix.ir/api/v2/orders/GetOrders", "json",
            ))

    # OCR pool + market-data sidecar pool (both comma/space lists).
    try:
        ocr_setting = await settings_store.get_setting(db, "ocr_service_url")
    except Exception:  # noqa: BLE001
        ocr_setting = ""
    for base in _ocr_base_urls(ocr_setting or get_settings().default_ocr_service_url):
        targets.append(Target(
            f"ocr:{base}", "ocr", base, base + "/", "any",
            host_local="host.docker.internal" in base,
        ))

    try:
        md_setting = await settings_store.get_setting(db, "bot_market_data_url")
    except Exception:  # noqa: BLE001
        md_setting = ""
    for base in _ocr_base_urls(md_setting):
        targets.append(Target(
            f"market-data:{base}", "market-data", base,
            base + f"/last-price?isin={PROBE_ISIN}", "json",
            host_local="host.docker.internal" in base,
        ))
    return targets


def build_probe_script(targets: list[Target], curl_timeout: int = 6) -> str:
    """Build a POSIX-sh script that probes every target in parallel, proxy-bypassed.

    Each target prints one ``key\\x1fcode|ctype|secs\\x1fmarker`` line (the marker
    is the body's first ~120 chars, control chars + ``|`` stripped). Concurrent
    single-line ``>>`` appends are < PIPE_BUF → atomic. ``key``/``url`` are
    ``shlex.quote``-d (the RLC url carries single quotes).
    """
    lines = [
        "set -u",
        "unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy "
        "NO_PROXY no_proxy 2>/dev/null || true",
        "d=$(mktemp -d) || exit 1",
        "probe() {",
        '  b="$d/$1"',
        (
            "  m=$(curl -ksS --noproxy '*' -m %d -A 'sm-monitor/1' -o \"$b\" "
            "-w '%%{http_code}|%%{content_type}|%%{time_total}' \"$3\" 2>/dev/null) "
            "|| m='000||'"
        ) % int(curl_timeout),
        "  k=$(head -c 400 \"$b\" 2>/dev/null | tr -d '\\000-\\037|')",
        "  printf '%s\\037%s\\037%s\\n' \"$2\" \"$m\" \"$k\" >> \"$d/out\"",
        "}",
    ]
    for i, t in enumerate(targets):
        lines.append(f"probe {i} {shlex.quote(t.key)} {shlex.quote(t.url)} &")
    lines += ["wait", 'cat "$d/out" 2>/dev/null', 'rm -rf "$d"']
    return "\n".join(lines)


def classify(
    expect: str, http_code: str, content_type: str, marker: str,
    isin: Optional[str] = None,
) -> str:
    """Classify one probe result → real | up | placeholder | degraded | down."""
    code = (http_code or "").strip()
    ct = (content_type or "").lower()
    m = (marker or "").lstrip()
    if not code or code == "000":
        return "down"
    if expect == "any":
        return "up"
    if expect == "jpeg":
        if "image" in ct:
            return "real"
        if code == "200" and "html" in ct:
            return "placeholder"
        return "degraded"
    if expect == "json_isin":
        # An exact ISIN match is the strongest signal, but the public RLC handler
        # returns a 200 JSON array (text/plain) whose `nc`/ISIN field can sit past
        # the truncated body marker — so a 200 non-HTML JSON-ish body is itself
        # proof the genuine handler answered (= real). Only a 200 HTML page is a
        # placeholder; a non-200 JSON-ish body is degraded.
        if isin and isin in (marker or ""):
            return "real"
        if "html" in ct:
            return "placeholder"
        if code == "200" and ("json" in ct or m[:1] in ("{", "[")):
            return "real"
        if "json" in ct or m[:1] in ("{", "["):
            return "degraded"
        return "degraded"
    # expect == "json"
    if "json" in ct:
        return "real"
    if code in ("400", "401", "403", "405", "429"):
        return "real"
    if code == "200" and "html" in ct:
        return "placeholder"
    if m[:1] in ("{", "["):
        return "real"
    return "degraded"


def _result(t: Target, state: str, *, http_status=None, content_type=None,
            latency_ms=None, detail=None) -> dict:
    return {
        "target_key": t.key, "group_name": t.group, "name": t.name, "url": t.url,
        "state": state, "http_status": http_status, "content_type": content_type,
        "latency_ms": latency_ms, "detail": detail,
    }


def _ssh_meta(state: str, detail: Optional[str] = None) -> dict:
    return {
        "target_key": _SSH_META_KEY, "group_name": "_meta", "name": "ssh", "url": "",
        "state": state, "http_status": None, "content_type": None,
        "latency_ms": None, "detail": detail,
    }


def _parse_probe_output(text: str) -> dict[str, tuple[str, str, str, str]]:
    out: dict[str, tuple[str, str, str, str]] = {}
    for line in (text or "").splitlines():
        if "\x1f" not in line:
            continue
        parts = line.split("\x1f")
        if len(parts) < 3:
            continue
        key, meta, marker = parts[0], parts[1], parts[2]
        mp = meta.split("|")
        code = mp[0] if len(mp) > 0 else ""
        ct = mp[1] if len(mp) > 1 else ""
        secs = mp[2] if len(mp) > 2 else ""
        out[key] = (code, ct, secs, marker)
    return out


async def probe_server(
    server, targets: list[Target], *, script_timeout: float = 20.0,
    curl_timeout: int = 6,
) -> list[dict]:
    """Probe every (non-host-local) target FROM ``server`` in one SSH round-trip.

    A transient SSH failure (cold pool / "Channel closed" right after a restart)
    is retried ONCE after a short pause before the whole server is declared down,
    so a one-off blip doesn't paint a healthy host's entire column red.
    """
    skipped = [
        _result(t, "skipped", detail="host-local (per-bot)")
        for t in targets if t.host_local
    ]
    remote = [t for t in targets if not t.host_local]
    if not remote:
        return skipped + [_ssh_meta("up")]

    script = build_probe_script(remote, curl_timeout)
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    cmd = "echo " + shlex.quote(b64) + " | base64 -d | sh"
    res = None
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            res = await run_command(server, cmd, timeout=script_timeout)
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001 — retry once, then all-down
            last_exc = exc
            if attempt == 0:
                await asyncio.sleep(_SSH_RETRY_DELAY)
    if last_exc is not None or res is None:
        return (
            skipped
            + [_result(t, "down", detail="host unreachable (ssh)") for t in remote]
            + [_ssh_meta("down", str(last_exc)[:120] if last_exc else "no response")]
        )

    parsed = _parse_probe_output(res.stdout)
    out = list(skipped)
    for t in remote:
        p = parsed.get(t.key)
        if p is None:
            out.append(_result(t, "degraded", detail="no probe output"))
            continue
        code, ct, secs, marker = p
        state = classify(t.expect, code, ct, marker, t.isin)
        try:
            latency = int(float(secs) * 1000) if secs else None
        except ValueError:
            latency = None
        try:
            http_status = int(code) if code and code != "000" else None
        except ValueError:
            http_status = None
        out.append(_result(
            t, state, http_status=http_status,
            content_type=(ct[:128] or None), latency_ms=latency,
            detail=(marker[:120] or None),
        ))
    out.append(_ssh_meta("up"))
    return out


async def record_results(
    db: AsyncSession, server_id: UUID, results: list[dict], *, prune_others: bool = False,
) -> None:
    """Upsert one round of probe results for a server (one row per target_key).

    ``prune_others=True`` (the full unauthenticated tick) also DELETES this
    server's non-auth rows whose ``target_key`` isn't in this round — so a
    disabled broker / removed OCR URL drops out of the matrix instead of sitting
    stale forever. Authenticated rows (refreshed only by the manual deep-check)
    are never pruned.
    """
    if not results:
        return
    now = datetime.now(timezone.utc)
    keys = [r["target_key"] for r in results]
    rows = [
        {
            "server_id": server_id, "target_key": r["target_key"],
            "group_name": r["group_name"], "name": r["name"], "url": r["url"],
            "state": r["state"], "http_status": r.get("http_status"),
            "content_type": r.get("content_type"), "latency_ms": r.get("latency_ms"),
            "detail": r.get("detail"), "probed_at": now,
        }
        for r in results
    ]
    stmt = pg_insert(ServiceProbeResult).values(rows)
    update_cols = [
        "group_name", "name", "url", "state", "http_status",
        "content_type", "latency_ms", "detail", "probed_at",
    ]
    stmt = stmt.on_conflict_do_update(
        index_elements=["server_id", "target_key"],
        set_={c: getattr(stmt.excluded, c) for c in update_cols},
    )
    await db.execute(stmt)
    if prune_others:
        await db.execute(
            delete(ServiceProbeResult).where(
                ServiceProbeResult.server_id == server_id,
                ServiceProbeResult.target_key.notin_(keys),
                ServiceProbeResult.group_name.notin_(_AUTH_GROUPS),
            )
        )
    await db.commit()


async def probe_all_once(
    db: AsyncSession, *, concurrency: int = 6, per_server_timeout: float = 50.0,
) -> int:
    """One unauthenticated tick: probe every server concurrently, upsert results.

    Each server records into its OWN session so one failure can't poison another.
    Returns the number of servers probed.
    """
    targets = await build_targets(db)
    servers = await list_servers(db)
    if not servers:
        return 0
    sem = asyncio.Semaphore(concurrency)

    async def _one(server) -> None:
        async with sem:
            try:
                results = await asyncio.wait_for(
                    probe_server(server, targets), timeout=per_server_timeout
                )
            except Exception as exc:  # noqa: BLE001
                results = (
                    [
                        _result(t, "skipped", detail="host-local (per-bot)")
                        if t.host_local
                        else _result(t, "down", detail="host unreachable (ssh)")
                        for t in targets
                    ]
                    + [_ssh_meta("down", str(exc)[:120])]
                )
            try:
                async with AsyncSessionLocal() as s:
                    await record_results(s, server.id, results, prune_others=True)
            except Exception:  # noqa: BLE001
                logger.exception("service_probe: record failed for %s", server.id)

    await asyncio.gather(*[_one(s) for s in servers])
    return len(servers)


# ---------------------------------------------------------------------------
# Authenticated tier (manual deep-check — Mostafa's credential)
# ---------------------------------------------------------------------------

# Inline probe run via ``docker exec -i <bot> python -c``. Reads JSON creds on
# stdin, reuses the BOT image's own broker code (same paths as cache_warmup), and
# prints ONE json line. Never raises (the bot module APIs are a soft dependency —
# a drift shows the auth row as down, never crashes the page/worker). ephoenix →
# login (identity+captcha+OCR) + get_instrument_info (hits marketdatagw); exir →
# adapter.prepare_order (login → buying power → RLC price → fee → volume), no order.
_AUTH_PROBE_SCRIPT = r'''
import sys, json, time
_d = json.load(sys.stdin)
_t0 = time.time()
def _out(ok, detail):
    print(json.dumps({"ok": bool(ok), "detail": str(detail)[:160],
                      "latency_ms": int((time.time() - _t0) * 1000)}))
try:
    fam = _d.get("family"); code = _d.get("broker"); user = _d.get("username")
    pw = _d.get("password"); isin = _d.get("isin")
    from captcha_utils import decode_captcha
    from cache_manager import TradingCache
    cache = TradingCache()
    if fam == "exir":
        from broker_adapters import get_adapter
        cs = {"broker": code, "username": user, "password": pw,
              "isin": isin, "side": "1", "broker_family": "exir"}
        adapter = get_adapter(code, username=user, password=pw,
                              config_section=cs, captcha_decoder=decode_captcha, cache=cache)
        prepared = adapter.prepare_order(isin=isin, side=1, config_section=cs)
        _out(True, "login+price ok price=%s vol=%s" % (
            getattr(prepared, "price", "?"), getattr(prepared, "volume", "?")))
    else:
        from broker_enum import get_endpoints_for
        from api_client import EphoenixAPIClient
        endpoints = get_endpoints_for(code)
        # The legacy ephoenix market-data host is decommissioned. Force the call to
        # the new shared host regardless of the bot image's (possibly stale)
        # endpoint map; ib keeps its own mdapi.ibtrader.ir shard.
        if code != "ib":
            endpoints["market_data"] = "https://marketdatagw.ephoenix.ir/api/v2/instruments/full"
        c = EphoenixAPIClient(broker_code=code, username=user, password=pw,
                              captcha_decoder=decode_captcha,
                              endpoints=endpoints, cache=cache)
        c.authenticate()
        info = c.get_instrument_info(isin, use_cache=False)
        sym = ""
        if isinstance(info, dict):
            sym = info.get("symbol") or info.get("title") or ""
        _out(True, "login+marketdata ok %s" % sym)
except Exception as e:
    _out(False, "%s: %s" % (type(e).__name__, e))
'''


def build_auth_probe_script(family: str) -> str:
    """The inline python the bot container runs. The body self-branches on the
    stdin ``family`` field, so the same script serves both — ``family`` is kept
    for clarity/validation."""
    return _AUTH_PROBE_SCRIPT


async def build_auth_targets(db: AsyncSession) -> list[AuthTarget]:
    """Resolve the monitor agent's customers → one auth target per (broker, account)."""
    agent_username = (get_settings().monitor_probe_agent_username or "").strip()
    if not agent_username:
        return []
    agent = (
        await db.execute(
            select(User).where(func.lower(User.username) == agent_username.lower())
        )
    ).scalars().first()
    if agent is None:
        logger.info("deep_check: monitor agent %r not found", agent_username)
        return []
    customers = list(
        (
            await db.execute(select(Customer).where(Customer.agent_id == agent.id))
        ).scalars().all()
    )
    brokers = {b.code: b for b in (await db.execute(select(Broker))).scalars().all()}
    out: list[AuthTarget] = []
    seen: set[tuple[str, str]] = set()
    for c in customers:
        key = (c.broker, c.username)
        if key in seen:
            continue
        seen.add(key)
        b = brokers.get(c.broker)
        family = b.family if b else "ephoenix"
        label = (b.label if (b and b.label) else c.broker)
        try:
            password = await services_customers.decrypt_password(c)
        except Exception:  # noqa: BLE001
            logger.warning("deep_check: decrypt failed for customer %s", c.id)
            continue
        out.append(AuthTarget(
            code=c.broker, family=family, label=label,
            username=c.username, password=password, isin=PROBE_ISIN,
        ))
    return out


async def _find_bot_container(server, *, timeout: float = 15.0) -> Optional[str]:
    """Name of a running bot container on the host, or None."""
    try:
        res = await run_command(
            server,
            "docker ps --format '{{.Names}}' | grep -m1 -E '^sm-agent-.*-bot$' || true",
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None
    names = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    return names[0] if names else None


def _auth_result(acct: AuthTarget, server, state: str, detail=None,
                 latency_ms=None) -> dict:
    return {
        # Account-specific key: targets are built per (broker, username), and the
        # upsert key is (server_id, target_key), so two accounts on the same
        # broker must NOT share a key or they'd overwrite each other.
        "target_key": f"auth:{acct.code}:{acct.username}",
        "group_name": f"auth-{acct.family}",
        "name": f"{acct.label} · {acct.username}",
        # Per-server result is the cell; keep the row label generic (not a single
        # server name, which is misleading on a per-server row).
        "url": "real login + market-data via bot container", "state": state,
        "http_status": None, "content_type": None,
        "latency_ms": latency_ms, "detail": detail,
    }


async def probe_server_auth(
    server, acct: AuthTarget, *, find_timeout: float = 15.0, auth_timeout: float = 90.0,
) -> dict:
    """Run a real login + API call for ``acct`` inside a bot container on ``server``."""
    try:
        bot = await _find_bot_container(server, timeout=find_timeout)
    except Exception:  # noqa: BLE001
        return _auth_result(acct, server, "down", "host unreachable (ssh)")
    if not bot:
        return _auth_result(acct, server, "skipped", "no bot container on host")

    script = build_auth_probe_script(acct.family)
    cmd = f"docker exec -i {shlex.quote(bot)} python -c {shlex.quote(script)}"
    creds = json.dumps({
        "family": acct.family, "broker": acct.code, "username": acct.username,
        "password": acct.password, "isin": acct.isin,
    }).encode("utf-8")
    try:
        res = await run_command(server, cmd, stdin_data=creds, timeout=auth_timeout)
    except Exception as exc:  # noqa: BLE001
        return _auth_result(acct, server, "down", f"exec failed: {str(exc)[:90]}")

    obj = None
    for line in reversed((res.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                break
            except ValueError:
                continue
    if obj is None:
        tail = (res.stderr or res.stdout or "").strip().replace("\n", " ")
        return _auth_result(acct, server, "degraded", f"no probe output: {tail[:90]}")
    latency = obj.get("latency_ms")
    detail = str(obj.get("detail") or ("login ok" if obj.get("ok") else "login failed"))[:120]
    state = "real" if obj.get("ok") else "down"
    return _auth_result(acct, server, state, detail, latency)


async def deep_check_once(actor_id: UUID, *, concurrency: int = 3) -> int:
    """Manual authenticated deep-check. Fire-and-forget friendly (own sessions).

    Serializes per ``(broker, account)`` — each account's servers are probed
    SEQUENTIALLY (never the same account from two IPs at once); different accounts
    run in parallel. A process-wide single-flight lock makes that guarantee hold
    ACROSS invocations too: a second run launched while one is in flight (e.g. a
    double-click) is skipped, so two runs can't log the same account in from two
    server IPs concurrently. Returns the number of accounts checked (0 if skipped).
    """
    lock = _get_deep_check_lock()
    if lock.locked():
        logger.info("deep_check: a run is already in flight — skipping this launch")
        return 0
    async with lock:
        async with AsyncSessionLocal() as db:
            targets = await build_auth_targets(db)
            servers = await list_servers(db)
        if not targets or not servers:
            logger.info(
                "deep_check: nothing to do (accounts=%d servers=%d)",
                len(targets), len(servers),
            )
            return 0

        sem = asyncio.Semaphore(concurrency)

        async def _chain(acct: AuthTarget) -> None:
            async with sem:
                for server in servers:
                    res = await probe_server_auth(server, acct)
                    try:
                        async with AsyncSessionLocal() as s:
                            await record_results(s, server.id, [res])
                    except Exception:  # noqa: BLE001
                        logger.exception("deep_check: record failed for %s", server.id)

        await asyncio.gather(*[_chain(t) for t in targets])

        try:
            async with AsyncSessionLocal() as s:
                s.add(AuditLog(
                    actor_user_id=actor_id, action="service_probe.deep_check",
                    target_type="service_probe", target_id="all",
                    before_json={},
                    after_json={"accounts": len(targets), "servers": len(servers)},
                ))
                await s.commit()
        except Exception:  # noqa: BLE001
            logger.exception("deep_check: audit write failed")
        logger.info(
            "deep_check done: %d accounts x %d servers", len(targets), len(servers)
        )
        return len(targets)


# ---------------------------------------------------------------------------
# Matrix for the page
# ---------------------------------------------------------------------------


async def build_service_matrix(db: AsyncSession) -> dict:
    """Shape stored probe results into the endpoint x server grid for the page."""
    rows = list((await db.execute(select(ServiceProbeResult))).scalars().all())
    servers = await list_servers(db)

    by: dict[tuple, ServiceProbeResult] = {}
    ssh_meta: dict = {}
    for r in rows:
        if r.target_key == _SSH_META_KEY:
            ssh_meta[r.server_id] = r
            continue
        by[(r.server_id, r.target_key)] = r

    cols = []
    for s in servers:
        meta = ssh_meta.get(s.id)
        cols.append({
            "id": s.id, "name": s.name, "host": s.host,
            "ssh_state": (meta.state if meta else None),
            "probed_at": (meta.probed_at if meta else None),
        })

    # Discover the target rows present (across any server), grouped + ordered.
    seen: dict[str, tuple[str, str, str]] = {}
    for r in rows:
        if r.group_name == "_meta":
            continue
        if r.target_key not in seen:
            seen[r.target_key] = (r.group_name, r.name, r.url)

    groups = []
    for gkey, glabel in GROUP_ORDER:
        grows = sorted(
            ((k, v[1], v[2]) for k, v in seen.items() if v[0] == gkey),
            key=lambda x: x[1],
        )
        if not grows:
            continue
        matrix_rows = []
        for key, name, url in grows:
            matrix_rows.append({
                "key": key, "name": name, "url": url,
                "cells": {s.id: by.get((s.id, key)) for s in servers},
            })
        groups.append({"group": gkey, "label": glabel, "rows": matrix_rows})

    body = [r for r in rows if r.group_name != "_meta"]
    return {
        "servers": cols,
        "groups": groups,
        "down": sum(1 for r in body if r.state == "down"),
        "placeholder": sum(1 for r in body if r.state in ("placeholder", "degraded")),
        "overall_freshness": max(
            (r.probed_at for r in rows if r.probed_at is not None), default=None
        ),
        "last_deep_check": max(
            (
                r.probed_at for r in rows
                if r.probed_at is not None
                and r.group_name in ("auth-ephoenix", "auth-exir")
            ),
            default=None,
        ),
        "server_count": len(servers),
        "row_count": len(seen),
    }
