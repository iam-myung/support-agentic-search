"""S9 auth RED contracts (REQ-010 / AC-012·013 permission; SPEC §3 roles, §4 OP-12, 401/403/404)."""

from __future__ import annotations

import importlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


def _fresh_app_client(monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    """Rebuild composition root after env changes (settings read at import/use)."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import support_platform.config as config_mod
    import support_platform.main as main_mod

    importlib.reload(config_mod)
    importlib.reload(main_mod)
    return TestClient(main_mod.app)


def test_auth_module_exposes_two_roles_only() -> None:
    try:
        from support_platform.auth import Role
    except ImportError as exc:
        raise AssertionError("support_platform.auth.Role must exist") from exc

    values = {m.value for m in Role}
    assert values == {"SUPPORT_AGENT", "KNOWLEDGE_ADMIN"}, f"unexpected roles: {values}"


def test_settings_exposes_phase1_api_mode() -> None:
    from support_platform.config import Settings

    assert "phase1_api_mode" in Settings.model_fields, (
        "Settings must expose phase1_api_mode for pilot v1 bypass control"
    )


def test_login_with_bad_credentials_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/support")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    client = _fresh_app_client(monkeypatch)
    resp = client.post(
        "/api/v2/auth/login",
        json={"username": "nobody", "password": "wrong"},
    )
    assert resp.status_code == 401, f"bad credentials must be 401, got {resp.status_code}"


def test_successful_login_sets_httponly_samesite_session_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GREEN will seed a known user; RED asserts cookie contract on 200 login."""
    try:
        from support_platform.auth.testing import ensure_user
    except ImportError as exc:
        raise AssertionError("support_platform.auth.testing.ensure_user required for login contract") from exc

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/support")
    ensure_user(username="agent1", password="secret-ok", role="SUPPORT_AGENT")
    client = _fresh_app_client(monkeypatch)
    resp = client.post(
        "/api/v2/auth/login",
        json={"username": "agent1", "password": "secret-ok"},
    )
    assert resp.status_code == 200, f"valid login must be 200, got {resp.status_code}"
    set_cookie = resp.headers.get("set-cookie") or ""
    assert "HttpOnly" in set_cookie or "httponly" in set_cookie.lower(), set_cookie
    assert "SameSite" in set_cookie or "samesite" in set_cookie.lower(), set_cookie


def test_unauthenticated_protected_v2_probe_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/support")
    client = _fresh_app_client(monkeypatch)
    inv_id = uuid4()
    resp = client.get(f"/api/v2/investigations/{inv_id}")
    assert resp.status_code == 401, f"unauthenticated v2 access must be 401, got {resp.status_code}"


def test_logout_invalidates_session(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from support_platform.auth.testing import ensure_user, login_session
    except ImportError as exc:
        raise AssertionError("auth.testing helpers required for logout contract") from exc

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/support")
    ensure_user(username="agent2", password="secret-ok", role="SUPPORT_AGENT")
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = login_session(client, username="agent2", password="secret-ok")
    out = client.post(
        "/api/v2/auth/logout",
        cookies=cookies,
        headers={"X-CSRF-Token": csrf},
    )
    assert out.status_code == 200, f"logout must succeed, got {out.status_code}"
    probe = client.get(f"/api/v2/investigations/{uuid4()}", cookies=cookies)
    assert probe.status_code == 401, f"session must be invalid after logout, got {probe.status_code}"


def test_invisible_investigation_returns_404_not_403() -> None:
    """SPEC §4: unauthorized visibility of another's investigation → 404 (no existence leak)."""
    try:
        from support_platform.auth.access import investigation_access_status
    except ImportError as exc:
        raise AssertionError("support_platform.auth.access.investigation_access_status missing") from exc

    status = investigation_access_status(
        viewer_user_id="11111111-1111-1111-1111-111111111111",
        owner_user_id="22222222-2222-2222-2222-222222222222",
        viewer_role="SUPPORT_AGENT",
        action="read",
    )
    assert status == 404, f"cross-user invisible investigation must be 404, got {status}"


def test_knowledge_admin_default_denied_reading_support_body_returns_403() -> None:
    """SPEC §3: KNOWLEDGE_ADMIN does not default-read support investigation bodies → 403."""
    try:
        from support_platform.auth.access import investigation_access_status
    except ImportError as exc:
        raise AssertionError("support_platform.auth.access.investigation_access_status missing") from exc

    owner = "11111111-1111-1111-1111-111111111111"
    status = investigation_access_status(
        viewer_user_id="33333333-3333-3333-3333-333333333333",
        owner_user_id=owner,
        viewer_role="KNOWLEDGE_ADMIN",
        action="read",
    )
    assert status == 403, f"KNOWLEDGE_ADMIN default read of support body must be 403, got {status}"


def test_write_without_csrf_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from support_platform.auth.testing import ensure_user, login_session
    except ImportError as exc:
        raise AssertionError("auth.testing helpers required for CSRF contract") from exc

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/support")
    ensure_user(username="agent3", password="secret-ok", role="SUPPORT_AGENT")
    client = _fresh_app_client(monkeypatch)
    cookies, _csrf = login_session(client, username="agent3", password="secret-ok")
    resp = client.post("/api/v2/auth/logout", cookies=cookies)
    assert resp.status_code in (401, 403), (
        f"write without CSRF must be rejected, got {resp.status_code}"
    )


def test_phase1_api_disabled_rejects_unauthenticated_bypass() -> None:
    """Pilot must close/restrict unauthenticated Phase 1 prototype API (SPEC §4)."""
    try:
        from support_platform.auth.phase1_guard import unauthenticated_phase1_status
    except ImportError as exc:
        raise AssertionError(
            "support_platform.auth.phase1_guard.unauthenticated_phase1_status missing"
        ) from exc

    disabled = unauthenticated_phase1_status("disabled")
    assert disabled in (401, 403, 404), (
        f"disabled mode must reject unauthenticated v1; got {disabled}"
    )
    assert unauthenticated_phase1_status("open") is None, (
        "open mode may allow Phase 1 local prototype without auth"
    )
