"""S0 config contract: DATABASE_URL required+parseable; LOG_LEVEL enum-only."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_missing_database_url_fails_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    from support_platform.config import load_settings

    with pytest.raises(ValidationError):
        load_settings()


def test_invalid_database_url_fails_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "not-a-valid-url")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    from support_platform.config import load_settings

    with pytest.raises(ValidationError):
        load_settings()


def test_invalid_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/support")
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")

    from support_platform.config import load_settings

    with pytest.raises(ValidationError):
        load_settings()


def test_valid_settings_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/support")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    from support_platform.config import load_settings

    settings = load_settings()
    assert hasattr(settings, "database_url"), "Settings must expose database_url"
    assert hasattr(settings, "log_level"), "Settings must expose log_level"
    assert str(settings.database_url).startswith("postgresql://")
    assert settings.log_level == "INFO"
