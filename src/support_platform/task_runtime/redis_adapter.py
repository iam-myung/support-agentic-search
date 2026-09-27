"""Redis wake/fanout adapter — never the sole task truth (SPEC §2/§6)."""

from __future__ import annotations

import socket
from typing import Any
from urllib.parse import urlparse
from uuid import UUID


WAKE_KEY = "support_platform:task_wakeup"


def _parse_host_port(redis_url: str) -> tuple[str, int]:
    parsed = urlparse(redis_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6379
    return host, port


class RedisWakeAdapter:
    """Best-effort wake queue; PG backlog scan remains authoritative."""

    def __init__(self, *, redis_url: str) -> None:
        self._redis_url = (redis_url or "").strip()

    def health(self) -> dict[str, Any]:
        if not self._redis_url:
            return {"ok": False, "degraded": True, "reason": "REDIS_URL empty"}
        host, port = _parse_host_port(self._redis_url)
        try:
            with socket.create_connection((host, port), timeout=0.4):
                pass
        except OSError as exc:
            return {"ok": False, "degraded": True, "reason": str(exc)}
        try:
            client = self._client()
            if client is None:
                return {"ok": True, "degraded": False, "note": "socket_ok_no_redis_pkg"}
            client.ping()
            return {"ok": True, "degraded": False}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "degraded": True, "reason": str(exc)}

    def _client(self):  # type: ignore[no-untyped-def]
        try:
            import redis
        except ImportError:
            return None
        return redis.Redis.from_url(self._redis_url, socket_connect_timeout=0.4)

    def enqueue_wake(self, *, investigation_id: str | UUID) -> bool | None:
        """Push wake hint. On failure return False/None — never delete PG tasks."""
        try:
            client = self._client()
            if client is None:
                # No redis package or unreachable — soft-fail.
                host, port = _parse_host_port(self._redis_url)
                with socket.create_connection((host, port), timeout=0.4):
                    pass
                return False
            client.rpush(WAKE_KEY, str(investigation_id))
            return True
        except Exception:  # noqa: BLE001
            return False

    def flush_wakeup_queue(self) -> None:
        """Simulate Redis key loss (tests / ops). Soft no-op if Redis unavailable."""
        try:
            client = self._client()
            if client is None:
                return
            client.delete(WAKE_KEY)
        except Exception:  # noqa: BLE001
            return
