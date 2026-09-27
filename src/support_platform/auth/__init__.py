"""Single-organization auth: roles, sessions, and access checks (Phase 2 / S9)."""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    SUPPORT_AGENT = "SUPPORT_AGENT"
    KNOWLEDGE_ADMIN = "KNOWLEDGE_ADMIN"


SESSION_COOKIE = "sp_session"
CSRF_HEADER = "X-CSRF-Token"

__all__ = ["Role", "SESSION_COOKIE", "CSRF_HEADER"]
