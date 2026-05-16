from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

VERSION = "0.1.0"


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe — does not touch the database."""
    return {"status": "ok", "version": VERSION}


@router.get("/ready")
async def ready(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Readiness probe — verifies database connectivity."""
    try:
        result = await db.execute(text("SELECT 1"))
        value = result.scalar()
        if value != 1:
            raise RuntimeError(f"Unexpected SELECT 1 result: {value!r}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness check failed", exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "version": VERSION, "error": "database not ready"},
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ready", "version": VERSION},
    )
