"""Shared pytest fixtures for the mgmt_ui test suite.

Notes
-----
* Required env vars (``DATABASE_URL`` / ``MGMT_SECRET_KEY`` / ``MGMT_FERNET_KEY_PART1``)
  are seeded at import time — before any ``from app...`` import triggers
  :func:`app.settings.get_settings`. The Fernet key part1 is a valid 32-byte
  url-safe base64 placeholder; the part2 file path is deliberately one that
  won't exist on dev boxes so :func:`app.security.crypto._load_full_key`
  takes its insecure-dev fallback branch (we never actually decrypt anything
  in unit tests).
* The health worker is force-disabled so the FastAPI startup hook is a no-op
  if ``create_app`` ever gets invoked by a test.
"""

from __future__ import annotations

import base64
import os

# ---------------------------------------------------------------------------
# Env wiring — MUST happen before any `from app...` import.
# ---------------------------------------------------------------------------
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost:5432/test",
)
os.environ.setdefault("MGMT_SECRET_KEY", "test-secret-key-do-not-use-in-prod")
# 32 zero-bytes is a syntactically valid Fernet key part — that's all we need
# here since unit tests never invoke encrypt/decrypt.
os.environ.setdefault(
    "MGMT_FERNET_KEY_PART1",
    base64.urlsafe_b64encode(b"\x00" * 32).decode("ascii"),
)
os.environ.setdefault(
    "MGMT_FERNET_KEY_PART2_PATH",
    "/nonexistent/key.part2",
)
os.environ["ENABLE_HEALTH_WORKER"] = "false"
os.environ["ENABLE_TRADE_INGESTOR"] = "false"

import pytest  # noqa: E402  — imports must follow env wiring


@pytest.fixture(autouse=True)
def _disable_health_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-braces: re-assert the env vars inside every test."""
    monkeypatch.setenv("ENABLE_HEALTH_WORKER", "false")
    monkeypatch.setenv("ENABLE_TRADE_INGESTOR", "false")
