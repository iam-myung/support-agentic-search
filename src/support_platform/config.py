"""Application settings: required parseable DATABASE_URL; constrained LOG_LEVEL."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AnyUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
Phase1ApiMode = Literal["open", "disabled", "local_only"]


class Settings(BaseSettings):
    """Validated environment for the support platform process."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    database_url: AnyUrl = Field(..., description="PostgreSQL URL; parsed only, not connected in S0")
    log_level: LogLevel = Field(default="INFO")
    phase1_api_mode: Phase1ApiMode = Field(
        default="open",
        description="Pilot control for unauthenticated Phase 1 /api/investigations bypass",
    )
    config_root: Path | None = Field(
        default=None,
        description="OP-13 only allowed directory for Prompt template files (CONFIG_ROOT)",
    )
    redis_url: str | None = Field(
        default=None,
        description="Redis wake/fanout URL (REDIS_URL); never the sole task truth",
    )
    worker_lease_seconds: int = Field(
        default=60,
        ge=1,
        description="Worker lease TTL seconds (WORKER_LEASE_SECONDS)",
    )


def load_settings() -> Settings:
    """Load settings from the process environment (no .env auto-load in S0)."""
    return Settings()
