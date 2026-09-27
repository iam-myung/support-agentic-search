"""In-memory / PostgreSQL Trace recorder (whitelist-only).

Postgres path stores TRACE_STEP rows in audit_events (SPEC §3) with whitelist payload.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from support_platform.audit.sanitize import has_forbidden_trace_keys, sanitize_trace_fields

_MEMORY_TRACES: list[dict[str, Any]] = []
TRACE_ACTION = "TRACE_STEP"


class TraceRecorder:
    """Append-only Trace store. Ordinary Trace never carries raw Q/docs/secrets/reasoning."""

    def __init__(self, backend: str = "memory", database_url: str | None = None) -> None:
        if backend not in {"memory", "postgres"}:
            raise ValueError(f"unsupported Trace backend: {backend}")
        if backend == "postgres" and not database_url:
            raise ValueError("postgres TraceRecorder requires database_url")
        self.backend = backend
        self.database_url = database_url

    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError("Trace payload must be a dict")
        if has_forbidden_trace_keys(payload):
            raise ValueError(
                "Trace rejects raw question/docs/secrets/reasoning; use whitelist fields only"
            )
        cleaned = sanitize_trace_fields(payload)
        if "investigation_id" not in cleaned or "request_id" not in cleaned:
            raise ValueError("Trace requires investigation_id and request_id")
        row = dict(cleaned)
        row.setdefault("_trace_id", str(uuid4()))
        if self.backend == "memory":
            _MEMORY_TRACES.append(row)
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
                    "id": row["_trace_id"],
                    "action": TRACE_ACTION,
                    "actor": str(row.get("actor") or "system"),
                    "iid": row["investigation_id"],
                    "rid": row["request_id"],
                    "payload": json.dumps(
                        {k: v for k, v in row.items() if k != "_trace_id"},
                        ensure_ascii=False,
                    ),
                },
            )
        return row

    def list_for_investigation(self, investigation_id: str) -> list[dict[str, Any]]:
        iid = str(investigation_id)
        if self.backend == "memory":
            return [dict(r) for r in _MEMORY_TRACES if str(r.get("investigation_id")) == iid]

        from sqlalchemy import create_engine, text

        engine = create_engine(self.database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT payload
                    FROM audit_events
                    WHERE investigation_id = CAST(:iid AS uuid)
                      AND action = :action
                    ORDER BY created_at ASC
                    """
                ),
                {"iid": iid, "action": TRACE_ACTION},
            ).mappings().all()
        out: list[dict[str, Any]] = []
        for r in rows:
            payload = r["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            out.append(dict(payload or {}))
        return out


def record_trace(
    payload: dict[str, Any],
    *,
    backend: str = "memory",
    database_url: str | None = None,
) -> dict[str, Any]:
    """Module-level helper used by investigation/task_runtime write hooks."""
    return TraceRecorder(backend=backend, database_url=database_url).record(payload)
