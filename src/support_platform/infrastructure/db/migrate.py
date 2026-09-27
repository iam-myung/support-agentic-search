"""Alembic upgrade entry for Phase 1 schema."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def _alembic_config(database_url: str | None = None) -> Config:
    root = Path(__file__).resolve().parents[4]
    ini_path = root / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(root / "migrations"))
    if database_url:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def upgrade_head(database_url: str | None = None) -> None:
    """Run Alembic upgrade to head against DATABASE_URL or provided URL."""
    command.upgrade(_alembic_config(database_url), "head")


upgrade_head.is_implemented = True  # type: ignore[attr-defined]
