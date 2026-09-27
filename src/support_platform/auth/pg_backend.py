"""PostgreSQL-backed user/session persistence for pilot auth (real-path SMOKE)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

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


def _engine(database_url: str):
    return create_engine(database_url, pool_pre_ping=True)


def upsert_user(*, database_url: str, username: str, password: str, role: str) -> UserRecord:
    pwd = hash_password(password)
    with _engine(database_url).begin() as conn:
        row = conn.execute(
            text("SELECT id, password_hash, role, is_active FROM users WHERE username = :u"),
            {"u": username},
        ).fetchone()
        if row is None:
            uid = uuid4()
            conn.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, role, is_active) "
                    "VALUES (:id, :u, :ph, :role, true)"
                ),
                {"id": uid, "u": username, "ph": pwd, "role": role},
            )
            return UserRecord(id=uid, username=username, password_hash=pwd, role=role, is_active=True)
        conn.execute(
            text(
                "UPDATE users SET password_hash = :ph, role = :role, is_active = true "
                "WHERE username = :u"
            ),
            {"ph": pwd, "role": role, "u": username},
        )
        return UserRecord(
            id=row[0],
            username=username,
            password_hash=pwd,
            role=role,
            is_active=True,
        )


def authenticate(*, database_url: str, username: str, password: str) -> UserRecord | None:
    with _engine(database_url).connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, username, password_hash, role, is_active FROM users "
                "WHERE username = :u"
            ),
            {"u": username},
        ).fetchone()
    if row is None or not row[4]:
        return None
    if not verify_password(password, row[2]):
        return None
    return UserRecord(id=row[0], username=row[1], password_hash=row[2], role=row[3], is_active=True)


def create_session(
    *, database_url: str, user: UserRecord, ttl_hours: int = 12
) -> tuple[str, SessionRecord]:
    raw_token = uuid4().hex + uuid4().hex
    csrf = uuid4().hex
    record = SessionRecord(
        id=uuid4(),
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
        csrf_token=csrf,
    )
    with _engine(database_url).begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sessions (id, user_id, token_hash, expires_at, csrf_token) "
                "VALUES (:id, :uid, :th, :exp, :csrf)"
            ),
            {
                "id": record.id,
                "uid": record.user_id,
                "th": record.token_hash,
                "exp": record.expires_at,
                "csrf": record.csrf_token,
            },
        )
    return raw_token, record


def get_session(*, database_url: str, raw_token: str | None) -> SessionRecord | None:
    if not raw_token:
        return None
    th = hash_token(raw_token)
    with _engine(database_url).connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, user_id, token_hash, expires_at, csrf_token FROM sessions "
                "WHERE token_hash = :th"
            ),
            {"th": th},
        ).fetchone()
    if row is None:
        return None
    expires = row[3]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        revoke_session(database_url=database_url, raw_token=raw_token)
        return None
    return SessionRecord(
        id=row[0],
        user_id=row[1],
        token_hash=row[2],
        expires_at=expires,
        csrf_token=row[4],
    )


def revoke_session(*, database_url: str, raw_token: str | None) -> bool:
    if not raw_token:
        return False
    th = hash_token(raw_token)
    with _engine(database_url).begin() as conn:
        result = conn.execute(text("DELETE FROM sessions WHERE token_hash = :th"), {"th": th})
    return bool(result.rowcount)


def delete_user_by_username(*, database_url: str, username: str) -> None:
    with _engine(database_url).begin() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE username = :u"), {"u": username}
        ).fetchone()
        if row is None:
            return
        conn.execute(text("DELETE FROM sessions WHERE user_id = :id"), {"id": row[0]})
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": row[0]})


def count_sessions_for_user(*, database_url: str, user_id: UUID) -> int:
    with _engine(database_url).connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM sessions WHERE user_id = :id"),
                {"id": user_id},
            ).scalar_one()
        )
