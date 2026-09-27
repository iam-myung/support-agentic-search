"""User/session store facade: memory (unit) or PostgreSQL (pilot / SMOKE)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from support_platform.auth.passwords import hash_password, hash_token, verify_password


@dataclass
class UserRecord:
    id: UUID
    username: str
    password_hash: str
    role: str
    is_active: bool = True


@dataclass
class SessionRecord:
    id: UUID
    user_id: UUID
    token_hash: str
    expires_at: datetime
    csrf_token: str


_USERS_BY_NAME: dict[str, UserRecord] = {}
_SESSIONS_BY_TOKEN_HASH: dict[str, SessionRecord] = {}


def use_postgres() -> bool:
    return os.environ.get("AUTH_BACKEND", "memory").strip().lower() == "postgres"


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL required when AUTH_BACKEND=postgres")
    return url


def reset_store() -> None:
    _USERS_BY_NAME.clear()
    _SESSIONS_BY_TOKEN_HASH.clear()


def upsert_user(*, username: str, password: str, role: str) -> UserRecord:
    if use_postgres():
        from support_platform.auth import pg_backend

        row = pg_backend.upsert_user(
            database_url=_database_url(), username=username, password=password, role=role
        )
        return UserRecord(
            id=row.id,
            username=row.username,
            password_hash=row.password_hash,
            role=row.role,
            is_active=row.is_active,
        )
    existing = _USERS_BY_NAME.get(username)
    if existing is not None:
        existing.password_hash = hash_password(password)
        existing.role = role
        existing.is_active = True
        return existing
    user = UserRecord(
        id=uuid4(),
        username=username,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    _USERS_BY_NAME[username] = user
    return user


def authenticate(username: str, password: str) -> UserRecord | None:
    if use_postgres():
        from support_platform.auth import pg_backend

        row = pg_backend.authenticate(
            database_url=_database_url(), username=username, password=password
        )
        if row is None:
            return None
        return UserRecord(
            id=row.id,
            username=row.username,
            password_hash=row.password_hash,
            role=row.role,
            is_active=row.is_active,
        )
    user = _USERS_BY_NAME.get(username)
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def create_session(user: UserRecord, *, ttl_hours: int = 12) -> tuple[str, SessionRecord]:
    if use_postgres():
        from support_platform.auth import pg_backend

        raw, row = pg_backend.create_session(
            database_url=_database_url(),
            user=pg_backend.UserRecord(
                id=user.id,
                username=user.username,
                password_hash=user.password_hash,
                role=user.role,
                is_active=user.is_active,
            ),
            ttl_hours=ttl_hours,
        )
        return raw, SessionRecord(
            id=row.id,
            user_id=row.user_id,
            token_hash=row.token_hash,
            expires_at=row.expires_at,
            csrf_token=row.csrf_token,
        )
    raw_token = uuid4().hex + uuid4().hex
    csrf = uuid4().hex
    record = SessionRecord(
        id=uuid4(),
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
        csrf_token=csrf,
    )
    _SESSIONS_BY_TOKEN_HASH[record.token_hash] = record
    return raw_token, record


def get_session(raw_token: str | None) -> SessionRecord | None:
    if use_postgres():
        from support_platform.auth import pg_backend

        row = pg_backend.get_session(database_url=_database_url(), raw_token=raw_token)
        if row is None:
            return None
        return SessionRecord(
            id=row.id,
            user_id=row.user_id,
            token_hash=row.token_hash,
            expires_at=row.expires_at,
            csrf_token=row.csrf_token,
        )
    if not raw_token:
        return None
    record = _SESSIONS_BY_TOKEN_HASH.get(hash_token(raw_token))
    if record is None:
        return None
    if record.expires_at <= datetime.now(timezone.utc):
        _SESSIONS_BY_TOKEN_HASH.pop(record.token_hash, None)
        return None
    return record


def revoke_session(raw_token: str | None) -> bool:
    if use_postgres():
        from support_platform.auth import pg_backend

        return pg_backend.revoke_session(database_url=_database_url(), raw_token=raw_token)
    if not raw_token:
        return False
    key = hash_token(raw_token)
    return _SESSIONS_BY_TOKEN_HASH.pop(key, None) is not None


def get_user(user_id: UUID) -> UserRecord | None:
    if use_postgres():
        from support_platform.auth import pg_backend
        from sqlalchemy import create_engine, text

        with create_engine(_database_url(), pool_pre_ping=True).connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, username, password_hash, role, is_active FROM users "
                    "WHERE id = :id"
                ),
                {"id": user_id},
            ).fetchone()
        if row is None:
            return None
        return UserRecord(
            id=row[0], username=row[1], password_hash=row[2], role=row[3], is_active=row[4]
        )
    for user in _USERS_BY_NAME.values():
        if user.id == user_id:
            return user
    return None
