"""S0 health contract: GET /health/live → 200 {status: alive}; no DB I/O."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


def test_health_live_returns_alive_json() -> None:
    from support_platform.main import app

    client = TestClient(app)
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_health_live_does_not_connect_to_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live must not open a DB connection (S0: process liveness only)."""
    from support_platform import main as main_mod

    def _forbid_connect(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("GET /health/live must not connect to the database")

    # Tripwire: if GREEN/impl wires a connect helper or engine, contacting it must fail this test.
    for attr in ("connect_database", "create_engine", "get_connection", "engine"):
        if hasattr(main_mod, attr):
            monkeypatch.setattr(main_mod, attr, _forbid_connect)

    client = TestClient(main_mod.app)
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
