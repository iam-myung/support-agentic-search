"""Test/seed helpers for auth contracts (not a production admin UI)."""

from __future__ import annotations

from typing import Any

from support_platform.auth import SESSION_COOKIE
from support_platform.auth.store import upsert_user


def ensure_user(*, username: str, password: str, role: str) -> None:
    upsert_user(username=username, password=password, role=role)


def login_session(client: Any, *, username: str, password: str) -> tuple[dict[str, str], str]:
    resp = client.post(
        "/api/v2/auth/login",
        json={"username": username, "password": password},
    )
    if resp.status_code != 200:
        raise AssertionError(f"login_session failed: {resp.status_code} {resp.text}")
    token = resp.cookies.get(SESSION_COOKIE)
    if not token:
        raise AssertionError("login_session missing session cookie")
    csrf = resp.json().get("csrf_token")
    if not csrf:
        raise AssertionError("login_session missing csrf_token in body")
    return {SESSION_COOKIE: token}, str(csrf)
