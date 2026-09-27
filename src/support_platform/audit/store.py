"""Append-only sanitized audit_events store (memory + PostgreSQL)."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from support_platform.audit.sanitize import sanitize_audit_payload

_MEMORY_AUDITS: list[dict[str, Any]] = []


class AuditStore:
    """Sanitized action audit (login failure, HITL decisions, etc.)."""

    def __init__(self, backend: str = "memory", database_url: str | None = None) -> None:
        if backend not in {"memory", "postgres"}:
            raise ValueError(f"unsupported AuditStore backend: {backend}")
        if backend == "postgres" and not database_url:
            raise ValueError("postgres AuditStore requires database_url")
        self.backend = backend
        self.database_url = database_url

    def append(
        self,
        *,
        action: str,
        actor: str,
        investigation_id: str | None,
        request_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "id": str(uuid4()),
            "action": str(action),
            "actor": str(actor),
            "investigation_id": str(investigation_id) if investigation_id else None,
            "request_id": str(request_id),
            "payload": sanitize_audit_payload(payload),
        }
        if self.backend == "memory":
            _MEMORY_AUDITS.append(row)
            return row
        from sqlalchemy import create_engine, text

        engine = create_engine(self.database_url, pool_pre_ping=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO audit_events (
                      id, action, actor, investigation_id, request_id, payload
                    ) VALUES (
                      CAST(:id AS uuid), :action, :actor,
                      CAST(:iid AS uuid), :rid, CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "id": row["id"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "iid": row["investigation_id"],  # None → NULL
                    "rid": row["request_id"],
                    "payload": json.dumps(row["payload"], ensure_ascii=False),
                },
            )
        return row

    def list_for_investigation(self, investigation_id: str) -> list[dict[str, Any]]:
        iid = str(investigation_id)
        if self.backend == "memory":
            return [dict(r) for r in _MEMORY_AUDITS if str(r.get("investigation_id")) == iid]
        from sqlalchemy import create_engine, text

        engine = create_engine(self.database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id::text AS id, action, actor,
                           investigation_id::text AS investigation_id,
                           request_id, payload
                    FROM audit_events
                    WHERE investigation_id = CAST(:iid AS uuid)
                    ORDER BY created_at ASC
                    """
                ),
                {"iid": iid},
            ).mappings().all()
        return [_row_to_dict(r) for r in rows]

    def list_by_action(self, action: str) -> list[dict[str, Any]]:
        act = str(action)
        if self.backend == "memory":
            return [dict(r) for r in _MEMORY_AUDITS if r.get("action") == act]
        from sqlalchemy import create_engine, text

        engine = create_engine(self.database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id::text AS id, action, actor,
                           investigation_id::text AS investigation_id,
                           request_id, payload
                    FROM audit_events
                    WHERE action = :action
                    ORDER BY created_at ASC
                    """
                ),
                {"action": act},
            ).mappings().all()
        return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: Any) -> dict[str, Any]:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return {
        "id": row["id"],
        "action": row["action"],
        "actor": row["actor"],
        "investigation_id": row["investigation_id"],
        "request_id": row["request_id"],
        "payload": payload or {},
    }
