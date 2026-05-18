from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Database
    database_url: str = Field(..., alias="DATABASE_URL")

    # Security
    secret_key: SecretStr = Field(..., alias="MGMT_SECRET_KEY")
    fernet_key_part1: SecretStr = Field(..., alias="MGMT_FERNET_KEY_PART1")
    fernet_key_part2_path: str = Field(
        default="/etc/sm/key.part2",
        alias="MGMT_FERNET_KEY_PART2_PATH",
    )
    # CSRF protection (Phase 10). Used by ``app.security.csrf`` to HMAC-sign
    # the double-submit token. MUST be overridden in production via the
    # ``MGMT_CSRF_SECRET`` env var — the default below is a dev placeholder
    # that is intentionally long enough to satisfy the min_length guard but
    # publicly known, so a forgotten override fails closed on review.
    csrf_secret: str = Field(
        default="dev-csrf-secret-CHANGE-ME-min-32-bytes-xxxxxxxxxxxxxxxx",
        alias="MGMT_CSRF_SECRET",
        min_length=32,
    )

    # JWT
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 8  # 8 hours

    # External services
    default_ocr_service_url: str = "http://5.10.248.55:18080"

    # App metadata
    app_name: str = "Seller-Market Management"
    environment: str = "development"

    # Cookies
    cookie_secure: bool = False  # set True in production

    # Background workers
    enable_health_worker: bool = Field(default=True, alias="ENABLE_HEALTH_WORKER")
    enable_stack_health_worker: bool = Field(
        default=True, alias="ENABLE_STACK_HEALTH_WORKER"
    )
    enable_trade_ingestor: bool = Field(
        default=True, alias="ENABLE_TRADE_INGESTOR"
    )
    trade_ingest_interval_seconds: int = Field(
        default=30, alias="TRADE_INGEST_INTERVAL_SECONDS"
    )
    # Issue #62: ingestor for scheduled-run markers written by
    # SellerMarket/scheduler.py after each cron-fire of cache_warmup /
    # run_trading. Same shape as the trade ingestor, different remote
    # directory (``run_results/`` instead of ``order_results/``).
    enable_scheduled_run_ingestor: bool = Field(
        default=True, alias="ENABLE_SCHEDULED_RUN_INGESTOR"
    )
    scheduled_run_ingest_interval_seconds: int = Field(
        default=30, alias="SCHEDULED_RUN_INGEST_INTERVAL_SECONDS", ge=1
    )
    # Phase 8 background workers. Intervals are validated at parse time
    # so a misconfigured env var can't turn the worker into a tight
    # retry loop; retention days are validated >= 0 so a negative value
    # can't shift the janitor cutoff into the future (which would purge
    # fresh data on the next tick).
    enable_health_scanner: bool = Field(default=True, alias="ENABLE_HEALTH_SCANNER")
    health_scan_interval_seconds: int = Field(
        default=60, alias="HEALTH_SCAN_INTERVAL_SECONDS", ge=1
    )

    enable_janitor: bool = Field(default=True, alias="ENABLE_JANITOR")
    janitor_interval_seconds: int = Field(
        default=3600, alias="JANITOR_INTERVAL_SECONDS", ge=1
    )
    janitor_order_results_retention_days: int = Field(
        default=14, alias="JANITOR_ORDER_RESULTS_RETENTION_DAYS", ge=0
    )
    janitor_run_log_retention_days: int = Field(
        default=90, alias="JANITOR_RUN_LOG_RETENTION_DAYS", ge=0
    )
    janitor_health_signal_retention_days: int = Field(
        default=30, alias="JANITOR_HEALTH_SIGNAL_RETENTION_DAYS", ge=0
    )

    # Run logs (Phase 6). Captured stdout+stderr from each docker exec
    # run is archived under this directory as ``<run_id>.log`` with mode
    # 0600. Relative to the mgmt_ui working directory by default; set
    # ``RUN_LOGS_DIR`` in production to an absolute path on a volume
    # that's backed up alongside the database.
    run_logs_dir: str = Field(default="./run_logs", alias="RUN_LOGS_DIR")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()  # type: ignore[call-arg]
