"""WebSocket endpoint for live run-log streaming."""
from __future__ import annotations

import asyncio
import logging
import uuid
from uuid import UUID

from fastapi import APIRouter, Cookie, Query, WebSocket, WebSocketDisconnect
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketState

from app.db import AsyncSessionLocal
from app.models.users import User
from app.security.auth import decode_token
from app.security.ws_token import verify_ws_token
from app.services import run_executor
from app.services import runs as services_runs
from app.services.ssh.streaming import LineEvent, StreamingResult

logger = logging.getLogger(__name__)

router = APIRouter()


async def _resolve_user(token: str | None, db: AsyncSession) -> User | None:
    """Decode the JWT cookie and load the :class:`User`. Return ``None`` on any failure.

    We inline the JWT decode + DB lookup here rather than depending on
    :func:`app.security.deps.get_current_user` because that helper is wired
    to raise HTTP 401, which doesn't translate cleanly to a WebSocket close.
    The convention from :mod:`app.security.deps` (lookup by ``sub`` UUID,
    treat soft-deleted users as missing) is preserved.
    """
    if not token:
        return None
    try:
        payload = decode_token(token)
    except JWTError:
        return None
    except Exception:  # noqa: BLE001 — never let an auth bug leak past close
        return None
    subject = payload.get("sub")
    if not subject:
        return None
    try:
        user_id = uuid.UUID(subject)
    except (ValueError, TypeError):
        return None
    user = await db.get(User, user_id)
    if user is None:
        return None
    if user.deleted_at is not None:
        return None
    return user


@router.websocket("/ws/runs/{run_id}")
async def run_stream(
    websocket: WebSocket,
    run_id: UUID,
    access_token: str | None = Cookie(default=None),
    token: str | None = Query(default=None),
) -> None:
    """Live-stream the run's stdout/stderr to the browser.

    Auth (Phase 10): cookie alone is no longer sufficient. The client
    must also pass a short-lived JWT in the ``?token=...`` query
    string (minted by ``POST /auth/ws-token``). This protects against
    the "cross-origin page triggers a WS upgrade and the browser
    attaches the cookie" attack — CSRF middleware doesn't run on
    WS upgrades.

    Both must agree on the user id; mismatched / missing / expired
    ws-token → close 4401. The ws-token has a 30 s TTL so a leaked
    URL is short-lived.

    Access: admin sees all; agent sees only their own runs.
    """
    await websocket.accept()
    try:
        async with AsyncSessionLocal() as db:
            user = await _resolve_user(access_token, db)
            if user is None:
                # Cookie missing / invalid — close before doing anything else.
                await websocket.close(code=4401)
                return
            # Phase 10: require the short-lived ws-token AND verify
            # its sub matches the cookie-auth user.
            ws_user_id = verify_ws_token(token or "")
            if ws_user_id is None or ws_user_id != str(user.id):
                logger.info(
                    "ws_token_invalid run=%s cookie_user=%s",
                    run_id, user.id,
                )
                await websocket.close(code=4401)
                return
            run = await services_runs.get_run(db, run_id)
            if run is None or not services_runs.can_user_see_run(user, run):
                await websocket.close(code=4404)
                return
    except Exception:  # noqa: BLE001
        logger.exception("ws auth/lookup failed for run=%s", run_id)
        await websocket.close(code=1011)
        return

    queue = run_executor.subscribe(run_id)

    # Replay archived log on initial connect so the user sees what they missed.
    # NOTE: only the archive (post-finalize) covers historical bytes; for
    # in-flight runs the executor is publishing new lines into the queue, and
    # we'll deliver them in order after this replay.
    async with AsyncSessionLocal() as db:
        run = await services_runs.get_run(db, run_id)
        if run and run.log_blob_ref:
            archived = await services_runs.read_run_log(run)
            if archived:
                for line in archived.splitlines():
                    await websocket.send_text(line.decode("utf-8", errors="replace"))
        if run and run.status != "running":
            # Run already finished — send the terminal marker and close.
            await websocket.send_text(
                f"<run-stream> run finished: {run.status} "
                f"(exit_code={run.exit_code})"
            )
            await websocket.close(code=1000)
            return

    # Wrap every send in a short timeout so a wedged WebSocket (browser tab
    # throttled, half-dead TCP, slow proxy) doesn't pin the handler forever
    # while the executor keeps publishing into a queue nobody is draining.
    # 10 s per send is generous for a single line over a healthy LAN; failure
    # to send means the client is gone and we should bail.
    _SEND_TIMEOUT = 10.0

    async def _send(text: str) -> bool:
        """Send with timeout; return False on failure or wedged peer."""
        if websocket.client_state != WebSocketState.CONNECTED:
            return False
        try:
            await asyncio.wait_for(websocket.send_text(text), timeout=_SEND_TIMEOUT)
            return True
        except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
            return False

    try:
        while True:
            try:
                # Shorter heartbeat than before (was 60 s) so a paused browser
                # tab is detected within ~5 s and the handler can bail
                # instead of holding the queue + publishing pipeline open.
                item = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                if websocket.client_state != WebSocketState.CONNECTED:
                    return
                if not await _send("<run-stream> ping"):
                    return
                continue

            if isinstance(item, LineEvent):
                prefix = "" if item.stream == "stdout" else "!! "
                if not await _send(prefix + item.data.decode("utf-8", errors="replace")):
                    return
            elif isinstance(item, StreamingResult):
                await _send(f"<run-stream> done (exit_code={item.exit_code})")
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.close(code=1000)
                return
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        logger.exception("ws stream failed for run=%s", run_id)
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1011)
