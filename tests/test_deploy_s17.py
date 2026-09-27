"""S17 Docker pilot / health / backup-restore RED (REQ-010 / AC-013; PRD §4.3).

Focus (SPEC §6 Phase2 deploy; §8 S17; approved PLAN):
- GET /health/ready probes config + PostgreSQL + Redis; unavailable → 503
- GET /health/live stays process-only 200 (must not be used as ready)
- Dockerfile + compose: web and worker share one app image; depend on db/redis
- Backup script fails closed when backup dir unusable or DB unreachable
- Rollback runbook/script documents previous-image rollback
- Security contracts 401/403/404 remain required for AC-013 (probe wiring)
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[1]


def _fresh_client(monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(
        "DATABASE_URL",
        env.get("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/support"),
    )
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    import support_platform.config as config_mod
    import support_platform.main as main_mod

    importlib.reload(config_mod)
    importlib.reload(main_mod)
    return TestClient(main_mod.app)


def test_health_ready_route_exists() -> None:
    """SPEC §4: Phase 2 GET /health/ready must exist (not only live)."""
    from support_platform.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/health/ready" in paths, "GET /health/ready required for AC-013 / PRD §4.3"


def test_health_ready_returns_503_when_database_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ready must actually check PostgreSQL — unreachable DB → 503, not 200."""
    client = _fresh_client(
        monkeypatch,
        DATABASE_URL="postgresql://nobody:bad@127.0.0.1:1/nope",
        REDIS_URL="redis://127.0.0.1:6379/0",
    )
    resp = client.get("/health/ready")
    assert resp.status_code == 503, (
        f"unready dependencies must yield 503, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("status") in {"not_ready", "NOT_READY", "unready"}, (
        f"ready failure body must signal not_ready, got {body}"
    )


def test_health_live_still_200_when_ready_would_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """live must not impersonate ready (SPEC §4)."""
    client = _fresh_client(
        monkeypatch,
        DATABASE_URL="postgresql://nobody:bad@127.0.0.1:1/nope",
    )
    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json() == {"status": "alive"}


def test_dockerfile_exists_at_repo_root() -> None:
    dockerfile = _REPO / "Dockerfile"
    assert dockerfile.is_file(), "S17 requires fixed app Dockerfile at repo root"


def test_compose_defines_web_and_worker_same_image() -> None:
    compose_path = _REPO / "docker-compose.yml"
    assert compose_path.is_file()
    text = compose_path.read_text(encoding="utf-8")
    assert "web:" in text or "\n  web:" in text, "compose must define web service"
    assert "worker:" in text or "\n  worker:" in text, "compose must define worker service"
    # Same image: both build from Dockerfile or share image: tag
    assert "Dockerfile" in text or "image:" in text, (
        "web/worker must reference fixed image/Dockerfile"
    )
    # Dependency on db + redis for pilot topology
    assert "db" in text and "redis" in text


def test_backup_module_exposes_run_backup() -> None:
    try:
        from support_platform.deploy import backup as backup_mod
    except ImportError as exc:
        pytest.fail(f"S17 backup module missing: {exc}")
    assert hasattr(backup_mod, "run_backup"), "deploy.backup.run_backup required"


def test_backup_fails_when_target_dir_not_writable(tmp_path: Path) -> None:
    try:
        from support_platform.deploy.backup import run_backup
    except ImportError as exc:
        pytest.fail(f"S17 backup module missing: {exc}")

    # Portable unusable target: parent path is a file, so nested dir cannot be created.
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("blocked", encoding="utf-8")
    with pytest.raises((OSError, PermissionError, RuntimeError, SystemExit, ValueError)):
        run_backup(
            database_url="postgresql://user:pass@127.0.0.1:5432/support",
            backup_dir=blocker / "nested_out",
        )


def test_backup_fails_when_database_unreachable(tmp_path: Path) -> None:
    try:
        from support_platform.deploy.backup import run_backup
    except ImportError as exc:
        pytest.fail(f"S17 backup module missing: {exc}")

    out = tmp_path / "s17_out"
    out.mkdir()
    with pytest.raises((OSError, RuntimeError, SystemExit, ConnectionError, ValueError)):
        run_backup(
            database_url="postgresql://nobody:bad@127.0.0.1:1/nope",
            backup_dir=out,
        )


def test_rollback_runbook_or_script_exists() -> None:
    """Previous-image rollback must be documented or scripted in-repo (approved PLAN)."""
    candidates = [
        _REPO / "deploy" / "S17_RUNBOOK.md",
        _REPO / "deploy" / "rollback.py",
        _REPO / "scripts" / "s17_rollback.py",
        _REPO / "src" / "support_platform" / "deploy" / "rollback.py",
    ]
    found = [p for p in candidates if p.is_file()]
    assert found, (
        "expected rollback runbook/script under deploy/ or scripts/ "
        f"(checked {[str(p.relative_to(_REPO)) for p in candidates]})"
    )
    blob = "\n".join(p.read_text(encoding="utf-8") for p in found).lower()
    assert "image" in blob or "镜像" in blob, "rollback artifact must mention image/镜像"
    assert "rollback" in blob or "回退" in blob or "previous" in blob or "上一" in blob


def test_ac013_security_contracts_still_wired() -> None:
    """AC-013 item 4: 401/403/404 contracts must remain present (S9 suite)."""
    auth_tests = _REPO / "tests" / "test_auth.py"
    assert auth_tests.is_file()
    src = auth_tests.read_text(encoding="utf-8")
    assert "401" in src and "403" in src and "404" in src, (
        "test_auth.py must retain 401/403/404 assertions for AC-013 security gate"
    )
