"""Readiness probes for Phase 2 GET /health/ready (config + PG + Redis)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def probe_readiness(*, database_url: str, redis_url: str | None) -> dict[str, Any]:
    """Return ``{"ok": True}`` only when PostgreSQL and Redis both answer."""
    if not (database_url or "").strip():
        return {"ok": False, "reason": "database_url_missing"}
    if not (redis_url or "").strip():
        return {"ok": False, "reason": "redis_url_missing"}

    try:
        _probe_postgres(database_url)
    except Exception as exc:  # noqa: BLE001 - any connect failure → not ready
        return {"ok": False, "reason": "postgres_unreachable", "detail": type(exc).__name__}

    try:
        _probe_redis(redis_url.strip())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "redis_unreachable", "detail": type(exc).__name__}

    return {"ok": True}


def _normalize_pg_url(database_url: str) -> str:
    url = database_url.strip()
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def _probe_postgres(database_url: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(
        _normalize_pg_url(database_url),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def _probe_redis(redis_url: str) -> None:
    import redis

    parsed = urlparse(redis_url)
    if not parsed.hostname:
        raise ValueError("redis_url_host_missing")
    client = redis.Redis.from_url(redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)
    try:
        if client.ping() is not True:
            raise RuntimeError("redis_ping_failed")
    finally:
        client.close()
