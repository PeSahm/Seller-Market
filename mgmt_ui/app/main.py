from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.models.users import User
from app.routers import admin as admin_router
from app.routers import agent as agent_router
from app.routers import auth as auth_router
from app.routers import health as health_router
from app.routers.ws import run_stream as ws_run_stream
from app.security.csrf import CSRFMiddleware
from app.security.deps import get_current_user
from app.settings import get_settings
from app.workers.health import run_health_worker
from app.workers.health_scanner import run_health_scanner
from app.workers.janitor import run_janitor
from app.workers.stack_health import run_stack_health_worker
from app.workers.scheduled_run_ingestor import run_scheduled_run_ingest_worker
from app.workers.trade_ingestor import run_trade_ingest_worker
from app.workers.broker_order_reconciler import run_broker_order_reconcile_worker
from app.workers.fire_log_ingestor import run_fire_log_ingest_worker

logger = logging.getLogger(__name__)


def _wants_html(request: Request) -> bool:
    """Heuristic: is this a browser navigation (HTML) vs an API client?"""
    if request.headers.get("HX-Request", "").lower() == "true":
        return True
    accept = request.headers.get("accept", "")
    return "text/html" in accept.lower()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0")

    # CSRF protection (Phase 10). Registered BEFORE routers so the middleware
    # wraps every mutating request. The /auth/login POST is whitelisted inside
    # the middleware itself since the user has no session/cookie on first
    # submit; everything else under /admin/* and /agent/* requires a matching
    # double-submit token.
    app.add_middleware(
        CSRFMiddleware,
        secret=settings.csrf_secret.encode("utf-8"),
        cookie_secure=settings.cookie_secure,
    )

    # Static files (agent #4 owns the directory contents).
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(static_dir)),
            name="static",
        )
    else:
        logger.warning("static directory %s does not exist; skipping mount", static_dir)

    # Routers
    app.include_router(health_router.router)
    app.include_router(auth_router.router)
    app.include_router(admin_router.router)
    app.include_router(agent_router.router)
    app.include_router(ws_run_stream.router)

    # Root: route to admin or agent dashboard based on role.
    @app.get("/", include_in_schema=False)
    async def root(user: User = Depends(get_current_user)) -> RedirectResponse:
        if user.role == "admin":
            return RedirectResponse(url="/admin/dashboard")
        return RedirectResponse(url="/agent/dashboard")

    # Unified 401 handling: browsers get redirected to login, APIs get JSON.
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse | RedirectResponse:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED and _wants_html(request):
            # Skip redirect for the login page itself to avoid loops.
            if not request.url.path.startswith("/auth/"):
                response = RedirectResponse(
                    url="/auth/login",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
                if request.headers.get("HX-Request", "").lower() == "true":
                    response.headers["HX-Redirect"] = "/auth/login"
                return response
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None) or {},
        )

    @app.on_event("startup")
    async def _log_startup() -> None:
        # Do NOT reveal key material — only report whether parts are configured.
        part1_set = bool(settings.fernet_key_part1.get_secret_value())
        part2_present = os.path.exists(settings.fernet_key_part2_path)
        logger.info(
            "startup app=%s env=%s fernet_part1=%s fernet_part2=%s (path=%s)",
            settings.app_name,
            settings.environment,
            "set" if part1_set else "MISSING",
            "found" if part2_present else "missing(dev fallback)",
            settings.fernet_key_part2_path,
        )

    # --------------------------------------------------------------
    # Background workers
    # --------------------------------------------------------------
    # The health worker is a single in-process asyncio task. We park its
    # handle + stop event on the FastAPI ``app.state`` to keep this module
    # free of module-level globals (which matter when create_app() is called
    # more than once, e.g. in test harnesses).
    app.state.health_stop = asyncio.Event()
    app.state.health_task = None  # type: Optional[asyncio.Task[None]]
    # Sibling worker for per-stack ``docker compose ps`` polling. Same shape
    # as the server-health worker, just a different cadence and a different
    # status enum to update.
    app.state.stack_health_stop = asyncio.Event()
    app.state.stack_health_task = None  # type: Optional[asyncio.Task[None]]
    # Phase 7: background trade-result ingestor. Walks every stack every
    # ``trade_ingest_interval_seconds`` and pulls down any new order_result
    # files via the parallel ``services.trade_ingestor.ingest_stack_once``.
    app.state.trade_ingest_stop = asyncio.Event()
    app.state.trade_ingest_task = None  # type: Optional[asyncio.Task[None]]
    # Issue #62: parallel ingestor for scheduled-run markers the bot's
    # ``scheduler.py`` drops into ``run_results/`` after each cron fire.
    # Surfaces scheduled cache_warmup / run_trading invocations in the
    # mgmt UI's Runs list alongside manual button-clicks.
    app.state.scheduled_run_ingest_stop = asyncio.Event()
    app.state.scheduled_run_ingest_task = None  # type: Optional[asyncio.Task[None]]
    # Phase 8: background health-signal scanner. Polls every stack every
    # ``health_scan_interval_seconds`` and upserts health_signals rows via
    # ``services.health_signals.scan_stack_once``.
    app.state.health_scanner_stop = asyncio.Event()
    app.state.health_scanner_task = None  # type: Optional[asyncio.Task[None]]
    # Phase 8: background janitor. Sweeps old order_results / run_logs /
    # health_signals on a slow cadence via ``services.janitor.run_janitor_tick``.
    app.state.janitor_stop = asyncio.Event()
    app.state.janitor_task = None  # type: Optional[asyncio.Task[None]]
    # Bot report: daily broker-order reconciler. Pulls a rolling recent window
    # of GetOrders for every customer into broker_orders so the report stays
    # current. Off by default (external broker calls) — see settings.
    app.state.broker_order_reconcile_stop = asyncio.Event()
    app.state.broker_order_reconcile_task = None  # type: Optional[asyncio.Task[None]]
    # P3: bot fire-log ingestor. Pulls order_fires_*.jsonl into order_fires and
    # reconciles broker_orders.is_bot. Internal SSH only — default on.
    app.state.fire_log_ingest_stop = asyncio.Event()
    app.state.fire_log_ingest_task = None  # type: Optional[asyncio.Task[None]]

    @app.on_event("startup")
    async def _start_health_worker() -> None:
        if not settings.enable_health_worker:
            logger.info("health worker disabled via settings")
            return
        app.state.health_task = asyncio.create_task(
            run_health_worker(app.state.health_stop),
            name="health-worker",
        )

    @app.on_event("shutdown")
    async def _stop_health_worker() -> None:
        task: Optional[asyncio.Task[None]] = app.state.health_task
        if task is None:
            return
        app.state.health_stop.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("health worker did not stop in 5s; cancelling")
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @app.on_event("startup")
    async def _start_stack_health_worker() -> None:
        if not settings.enable_stack_health_worker:
            logger.info("stack health worker disabled via settings")
            return
        app.state.stack_health_task = asyncio.create_task(
            run_stack_health_worker(app.state.stack_health_stop),
            name="stack-health-worker",
        )

    @app.on_event("shutdown")
    async def _stop_stack_health_worker() -> None:
        task: Optional[asyncio.Task[None]] = app.state.stack_health_task
        if task is None:
            return
        app.state.stack_health_stop.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("stack health worker did not stop in 5s; cancelling")
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @app.on_event("startup")
    async def _start_trade_ingestor() -> None:
        if not settings.enable_trade_ingestor:
            logger.info("trade ingestor worker disabled via settings")
            return
        app.state.trade_ingest_task = asyncio.create_task(
            run_trade_ingest_worker(
                app.state.trade_ingest_stop,
                interval_seconds=settings.trade_ingest_interval_seconds,
            ),
            name="trade-ingest-worker",
        )

    @app.on_event("shutdown")
    async def _stop_trade_ingestor() -> None:
        task: Optional[asyncio.Task[None]] = app.state.trade_ingest_task
        if task is None:
            return
        app.state.trade_ingest_stop.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("trade ingestor worker did not stop in 5s; cancelling")
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @app.on_event("startup")
    async def _start_scheduled_run_ingestor() -> None:
        if not settings.enable_scheduled_run_ingestor:
            logger.info("scheduled-run ingestor worker disabled via settings")
            return
        app.state.scheduled_run_ingest_task = asyncio.create_task(
            run_scheduled_run_ingest_worker(
                app.state.scheduled_run_ingest_stop,
                interval_seconds=settings.scheduled_run_ingest_interval_seconds,
            ),
            name="scheduled-run-ingest-worker",
        )

    @app.on_event("shutdown")
    async def _stop_scheduled_run_ingestor() -> None:
        task: Optional[asyncio.Task[None]] = app.state.scheduled_run_ingest_task
        if task is None:
            return
        app.state.scheduled_run_ingest_stop.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("scheduled-run ingestor worker did not stop in 5s; cancelling")
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @app.on_event("startup")
    async def _start_fire_log_ingestor() -> None:
        if not settings.enable_fire_log_ingestor:
            logger.info("fire-log ingestor disabled via settings")
            return
        app.state.fire_log_ingest_stop = asyncio.Event()
        app.state.fire_log_ingest_task = asyncio.create_task(
            run_fire_log_ingest_worker(
                app.state.fire_log_ingest_stop,
                interval_seconds=settings.fire_log_ingest_interval_seconds,
            ),
            name="fire-log-ingest-worker",
        )

    @app.on_event("shutdown")
    async def _stop_fire_log_ingestor() -> None:
        task: Optional[asyncio.Task[None]] = app.state.fire_log_ingest_task
        if task is None:
            return
        app.state.fire_log_ingest_stop.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @app.on_event("startup")
    async def _start_broker_order_reconciler() -> None:
        if not settings.enable_broker_order_reconciler:
            logger.info("broker order reconciler disabled via settings")
            return
        # Fresh stop event so a second lifespan cycle on the same app instance
        # (e.g. tests) doesn't inherit the previous shutdown's signaled event
        # and exit the worker immediately.
        app.state.broker_order_reconcile_stop = asyncio.Event()
        app.state.broker_order_reconcile_task = asyncio.create_task(
            run_broker_order_reconcile_worker(
                app.state.broker_order_reconcile_stop,
                interval_seconds=settings.broker_order_reconcile_interval_seconds,
                lookback_days=settings.broker_order_reconcile_lookback_days,
            ),
            name="broker-order-reconcile-worker",
        )

    @app.on_event("shutdown")
    async def _stop_broker_order_reconciler() -> None:
        task: Optional[asyncio.Task[None]] = app.state.broker_order_reconcile_task
        if task is None:
            return
        app.state.broker_order_reconcile_stop.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @app.on_event("startup")
    async def _start_health_scanner() -> None:
        if not settings.enable_health_scanner:
            logger.info("health scanner worker disabled via settings")
            return
        app.state.health_scanner_task = asyncio.create_task(
            run_health_scanner(
                app.state.health_scanner_stop,
                interval_seconds=settings.health_scan_interval_seconds,
            ),
            name="health-scanner-worker",
        )

    @app.on_event("shutdown")
    async def _stop_health_scanner() -> None:
        task: Optional[asyncio.Task[None]] = app.state.health_scanner_task
        if task is None:
            return
        app.state.health_scanner_stop.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("health scanner worker did not stop in 5s; cancelling")
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @app.on_event("startup")
    async def _start_janitor() -> None:
        if not settings.enable_janitor:
            logger.info("janitor worker disabled via settings")
            return
        app.state.janitor_task = asyncio.create_task(
            run_janitor(
                app.state.janitor_stop,
                interval_seconds=settings.janitor_interval_seconds,
            ),
            name="janitor-worker",
        )

    @app.on_event("shutdown")
    async def _stop_janitor() -> None:
        task: Optional[asyncio.Task[None]] = app.state.janitor_task
        if task is None:
            return
        app.state.janitor_stop.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("janitor worker did not stop in 5s; cancelling")
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    return app


app = create_app()
