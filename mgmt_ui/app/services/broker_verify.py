"""Resilient credential verification with a Tebyan-host proxy fallback.

Some broker identity hosts are UNROUTABLE from the mgmt hosts (PouyanIt +
ParsPack) but reachable from the Tebyan trading hosts — the per-network routing
gap first found for ``marketdatagw`` (Session 29) and confirmed for the ``ideal``
broker (``identity-ideal.ephoenix.ir`` → 185.115.151.x / AS214751, Session 35):
a direct mgmt-side verify ``ConnectTimeout``s, so the customer is stuck
``transient`` even though the bots (on Tebyan) log in fine.

``verify_credentials_resilient`` is the single entry point shared by the daily
checker, the manual recheck, and the verify-on-save button. It:

1. Cheaply probes whether the broker is reachable from the mgmt host.
2. If reachable → verify mgmt-direct (the fast path, unchanged for the ~all
   brokers that work from mgmt); only on an INCONCLUSIVE (transient) verdict does
   it fall back to a trading host.
3. If unreachable → re-run the verification THROUGH a trading host's bot
   container (reusing the Session-32 deep-check SSH + ``docker exec`` path),
   which CAN reach the broker.

The in-container verify reuses the BOT image's own broker code (``api_client`` /
``broker_adapters`` / ``cred_errors``), already deployed fleet-wide (PR #168), so
this is a MGMT-ONLY change — the verify script is sent inline, never baked into
the bot image. The three-way :class:`CredStatus` verdict is preserved whichever
path runs, so every caller agrees.
"""
from __future__ import annotations

import json
import logging
import shlex
from typing import Optional

import httpx

from app.services import broker_client
from app.services.brokers.base import CredStatus, VerifyResult, resolve_cred_status
from app.services.brokers_admin import get_broker_by_code
from app.services.service_monitor import PROBE_ISIN, _find_bot_container
from app.services.servers import list_servers
from app.services.ssh.commands import run_command

logger = logging.getLogger(__name__)

# A reachability probe must be quick: a blocked host SYN-drops, so we only wait a
# few seconds before concluding "unreachable from mgmt → use a trading host".
_REACH_TIMEOUT = 4.0
# The in-container login (captcha + OCR + broker login, possibly + customer-info)
# can take a little while; bound it so a wedged host can't hang the request.
_PROXY_TIMEOUT = 90.0

# Inline python the bot container runs (``docker exec -i <bot> python -c``). Reads
# creds JSON on stdin, reuses the bot image's own broker code, prints ONE json
# line: ``{"status": "valid"|"invalid_credentials"|"transient", "detail": ...,
# [full_name/national_id/bourse_code/type]}``. A successful LOGIN is VALID even if
# the bonus customer-info call (a different host that may itself be network-blocked)
# fails — the login already proved the credentials. Never raises.
_CRED_VERIFY_SCRIPT = r'''
import sys, json
_d = json.load(sys.stdin)
def _out(status, detail, **extra):
    o = {"status": status, "detail": str(detail)[:160]}
    o.update({k: v for k, v in extra.items() if v is not None})
    print(json.dumps(o, ensure_ascii=False))
try:
    fam = _d.get("family"); code = _d.get("broker"); user = _d.get("username")
    pw = _d.get("password"); isin = _d.get("isin")
    from captcha_utils import decode_captcha
    from cache_manager import TradingCache
    from cred_errors import InvalidCredentialsError
    cache = TradingCache()
    if fam in ("exir", "mofid", "onlineplus"):
        from broker_adapters import get_adapter
        cs = {"broker": code, "username": user, "password": pw,
              "isin": isin, "side": "1", "broker_family": fam}
        adapter = get_adapter(code, username=user, password=pw,
                              config_section=cs, captcha_decoder=decode_captcha, cache=cache)
        try:
            # validate() = login + sizing, NO side effect (mofid's prepare_order
            # would create real draft orders; validate skips them).
            adapter.validate(isin=isin, side=1, config_section=cs)
            _out("valid", fam + " login ok")
        except InvalidCredentialsError as e:
            _out("invalid_credentials", str(e) or "broker rejected credentials")
    else:
        from broker_enum import get_endpoints_for
        from api_client import EphoenixAPIClient
        endpoints = get_endpoints_for(code)
        if code != "ib":
            endpoints["market_data"] = "https://marketdatagw.ephoenix.ir/api/v2/instruments/full"
        c = EphoenixAPIClient(broker_code=code, username=user, password=pw,
                              captcha_decoder=decode_captcha,
                              endpoints=endpoints, cache=cache)
        try:
            c.authenticate()
        except InvalidCredentialsError as e:
            _out("invalid_credentials", str(e) or "broker rejected credentials")
        else:
            fn = nid = bc = ty = None
            try:
                info = c.get_customer_info()
                if isinstance(info, dict):
                    fn = info.get("fullName"); nid = info.get("nationalId")
                    bc = info.get("bourseCode"); ty = info.get("type")
            except Exception:
                pass
            _out("valid", "login ok", full_name=fn, national_id=nid,
                 bourse_code=bc, type=ty)
except InvalidCredentialsError as e:
    _out("invalid_credentials", str(e) or "broker rejected credentials")
except Exception as e:
    _out("transient", "%s: %s" % (type(e).__name__, e))
'''


def _probe_url(broker_code: str, family: str) -> Optional[str]:
    """The cheapest "is this broker reachable" URL for a family, or None if we
    don't have a known probe URL (→ assume reachable, rely on transient→proxy)."""
    if family == "ephoenix":
        # Covers ib too (family ephoenix) — _endpoints_for routes ib to ibtrader.
        return broker_client._endpoints_for(broker_code)["captcha"]
    if family == "exir":
        return f"https://{broker_code}.exirbroker.com/captcha"
    if family == "mofid":
        return "https://api-mts.orbis.easytrader.ir/"
    return None


async def _reachable_from_mgmt(
    broker_code: str, family: str, *, timeout: float = _REACH_TIMEOUT
) -> bool:
    """True iff the broker's primary host answers (or even errors) from the mgmt
    host. Only a connect failure/timeout (the AS214751 SYN-drop) → unreachable;
    any HTTP response/redirect/read-timeout means it routed → reachable."""
    url = _probe_url(broker_code, family)
    if not url:
        return True
    try:
        async with httpx.AsyncClient(
            trust_env=False, timeout=timeout, verify=False
        ) as client:
            await client.get(url)
        return True
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return False
    except Exception:  # noqa: BLE001 — any other outcome means it routed
        return True


def _parse_json_line(text: str) -> Optional[dict]:
    """Return the last JSON object line printed by the in-container script."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _payload_to_result(obj: Optional[dict]) -> VerifyResult:
    """Map the in-container ``{status, detail, ...}`` payload to a VerifyResult."""
    if not isinstance(obj, dict):
        return VerifyResult(
            ok=False, status=CredStatus.TRANSIENT,
            error="no probe output from trading host",
        )
    status = (obj.get("status") or "").strip()
    detail = obj.get("detail") or ""
    if status == "valid":
        return VerifyResult(
            ok=True, status=CredStatus.VALID, message=detail,
            full_name=obj.get("full_name"), national_id=obj.get("national_id"),
            bourse_code=obj.get("bourse_code"), type_=obj.get("type"),
        )
    if status == "invalid_credentials":
        return VerifyResult(
            ok=False, status=CredStatus.INVALID_CREDENTIALS,
            error=detail or "broker rejected the credentials",
        )
    return VerifyResult(
        ok=False, status=CredStatus.TRANSIENT,
        error=detail or "verification inconclusive via trading host",
    )


async def verify_via_trading_host(
    *, db, broker_code: str, family: str, username: str, password: str,
    isin: Optional[str], label: Optional[str] = None,
) -> VerifyResult:
    """Run a real broker login for these credentials INSIDE a bot container on a
    trading host (which can reach the broker), returning a three-way VerifyResult.

    Iterates the managed servers: skips hosts without a running bot container,
    returns the first DECISIVE (valid/invalid) verdict, and — if a host's verdict
    is transient (it may itself not reach the broker / OCR blip) — falls through
    to the next host. If no host could decide, returns the last transient (or a
    clear "no host could verify" transient)."""
    try:
        servers = await list_servers(db)
    except Exception as exc:  # noqa: BLE001
        return VerifyResult(
            ok=False, status=CredStatus.TRANSIENT,
            error=f"could not list trading hosts: {str(exc)[:90]}",
        )
    creds = json.dumps({
        "family": family, "broker": broker_code, "username": username,
        "password": password, "isin": isin or PROBE_ISIN,
    }).encode("utf-8")

    last: Optional[VerifyResult] = None
    for server in servers:
        try:
            bot = await _find_bot_container(server)
        except Exception:  # noqa: BLE001
            bot = None
        if not bot:
            continue
        # NB: no str.format here — _CRED_VERIFY_SCRIPT contains literal { } braces.
        cmd = (
            "docker exec -i " + shlex.quote(bot)
            + " python -c " + shlex.quote(_CRED_VERIFY_SCRIPT)
        )
        try:
            res = await run_command(
                server, cmd, stdin_data=creds, timeout=_PROXY_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001 — try the next host
            last = VerifyResult(
                ok=False, status=CredStatus.TRANSIENT,
                error=f"verify via {server.host} failed: {str(exc)[:90]}",
            )
            continue
        result = _payload_to_result(_parse_json_line(res.stdout))
        if resolve_cred_status(result) != CredStatus.TRANSIENT:
            logger.info(
                "proxy verify for %s@%s via %s -> %s",
                username, broker_code, server.host, result.status.value,
            )
            return result
        last = result
    if last is not None:
        return last
    return VerifyResult(
        ok=False, status=CredStatus.TRANSIENT,
        error=(
            "no trading host with a running bot container could verify "
            f"{label or broker_code}"
        ),
    )


async def verify_credentials_resilient(
    *, db, broker_code: str, username: str, password: str,
    ocr_service_url: str, isin: Optional[str] = None,
) -> VerifyResult:
    """Verify credentials, transparently proxying through a trading host when the
    broker is unreachable from the mgmt host (or mgmt-direct is inconclusive).

    Drop-in for ``broker_client.verify_credentials`` (plus a ``db``), returning
    the same VerifyResult/CredStatus contract."""
    try:
        broker = await get_broker_by_code(db, broker_code)
    except Exception:  # noqa: BLE001
        broker = None
    family = broker.family if broker else "ephoenix"
    label = broker.label if (broker and broker.label) else broker_code

    if await _reachable_from_mgmt(broker_code, family):
        result = await broker_client.verify_credentials(
            broker_code=broker_code, username=username, password=password,
            ocr_service_url=ocr_service_url,
        )
        if resolve_cred_status(result) != CredStatus.TRANSIENT:
            return result
        # mgmt-direct couldn't decide (OCR/captcha/blip) — try a trading host.
        proxied = await verify_via_trading_host(
            db=db, broker_code=broker_code, family=family,
            username=username, password=password, isin=isin, label=label,
        )
        if resolve_cred_status(proxied) != CredStatus.TRANSIENT:
            return proxied
        return result

    logger.info(
        "broker %s unreachable from mgmt — verifying %s via a trading host",
        broker_code, username,
    )
    return await verify_via_trading_host(
        db=db, broker_code=broker_code, family=family,
        username=username, password=password, isin=isin, label=label,
    )


__all__ = ["verify_credentials_resilient", "verify_via_trading_host"]
