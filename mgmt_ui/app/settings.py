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
