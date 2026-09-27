"""OP-12 auth HTTP routes (login/logout). OP-07/08 live in task_runtime.routes."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from support_platform.audit.store import AuditStore
from support_platform.auth import CSRF_HEADER, SESSION_COOKIE
from support_platform.auth.store import (
    authenticate,
    create_session,
    get_session,
    revoke_session,
)

router = APIRouter(tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


def _audit_login_failure(username: str) -> None:
    """SPEC §4 OP-12: login failure writes sanitized audit (no password).

    Fail-closed: never prevent returning 401. Use postgres only when
    AUTH_BACKEND=postgres and DATABASE_URL is set; on any write error fall
    back to memory (BUG-S16-001).
    """
    import os

    from support_platform.auth.store import use_postgres

    database_url = os.environ.get("DATABASE_URL", "").strip()
    payload = {
        "action": "LOGIN_FAILURE",
        "actor": username or "unknown",
        "investigation_id": None,
        "request_id": str(uuid4()),
        "payload": {"username": username},
    }
    if use_postgres() and database_url:
        try:
            AuditStore(backend="postgres", database_url=database_url).append(**payload)
            return
        except Exception:  # noqa: BLE001 — audit must not break OP-12 401
            pass
    try:
        AuditStore(backend="memory").append(**payload)
    except Exception:  # noqa: BLE001
        pass


@router.post("/api/v2/auth/login")
def login(body: LoginBody) -> JSONResponse:
    user = authenticate(body.username.strip(), body.password)
    if user is None:
        _audit_login_failure(body.username.strip())
        return JSONResponse(status_code=401, content={"detail": "invalid credentials"})
    raw_token, session = create_session(user)
    response = JSONResponse(
        status_code=200,
        content={
            "user_id": str(user.id),
            "role": user.role,
            "csrf_token": session.csrf_token,
        },
    )
    response.set_cookie(
        key=SESSION_COOKIE,
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return response


@router.post("/api/v2/auth/logout")
def logout(request: Request) -> JSONResponse:
    raw_token = request.cookies.get(SESSION_COOKIE)
    session = get_session(raw_token)
    if session is None:
        return JSONResponse(status_code=401, content={"detail": "not authenticated"})
    csrf = request.headers.get(CSRF_HEADER)
    if not csrf or csrf != session.csrf_token:
        return JSONResponse(status_code=403, content={"detail": "csrf required"})
    revoke_session(raw_token)
    response = JSONResponse(status_code=200, content={"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
