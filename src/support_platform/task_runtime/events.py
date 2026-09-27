"""Investigation event persistence + public payload sanitization (SPEC §4 OP-09)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

ALLOWED_EVENT_TYPES = frozenset(
    {
        "QUEUED",
        "STARTED",
        "STEP",
        "REVIEW_REQUIRED",
        "ANSWER_CHUNK",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    }
)

_BLOCKED_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "llm_api_key",
        "internal_reasoning",
        "internal",
        "reasoning",
        "cot",
        "chain_of_thought",
        "system_prompt",
        "prompt",
        "raw_retrieval",
        "unauthorized_source",
        "secret",
        "password",
        "token",
    }
)


def _engine(database_url: str):
    return create_engine(database_url, pool_pre_ping=True)


def sanitize_public_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Drop secrets / internal fields from SSE public payloads."""
    if not payload:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        lk = str(key).lower().replace("-", "_")
        if lk in _BLOCKED_KEYS or any(b in lk for b in ("api_key", "secret", "password", "token")):
            continue
        if isinstance(value, str):
            low = value.lower()
            if "sk-" in low or "chain of thought" in low or "internal reasoning" in low:
                continue
            if "system prompt" in low or "api_key" in low:
                continue
        if isinstance(value, dict):
            cleaned[key] = sanitize_public_payload(value)
        elif isinstance(value, list):
            # Never stream raw retrieval dumps.
            if lk in {"hits", "docs", "chunks", "raw"}:
                continue
            cleaned[key] = value
        else:
            cleaned[key] = value
    return cleaned


# Alias expected by RED probes.
public_payload_only = sanitize_public_payload


def max_sequence(*, database_url: str, investigation_id: str | UUID) -> int:
    with _engine(database_url).connect() as conn:
        n = conn.execute(
            text(
                """
                SELECT COALESCE(MAX(sequence), 0)
                FROM investigation_events
                WHERE investigation_id = CAST(:iid AS uuid)
                """
            ),
            {"iid": str(investigation_id)},
        ).scalar_one()
    return int(n or 0)


def append_event(
    *,
    database_url: str,
    investigation_id: str | UUID,
    event_type: str,
    public_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a sanitized public event with the next per-task sequence."""
    typ = (event_type or "").strip().upper()
    if typ not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"invalid event type: {event_type}")
    if typ == "ANSWER_CHUNK":
        raise ValueError("ANSWER_CHUNK requires append_answer_chunk validation gate")

    payload = sanitize_public_payload(public_payload)
    iid = str(investigation_id)
    with _engine(database_url).begin() as conn:
        nxt = conn.execute(
            text(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM investigation_events
                WHERE investigation_id = CAST(:iid AS uuid)
                """
            ),
            {"iid": iid},
        ).scalar_one()
        seq = int(nxt)
        eid = str(uuid4())
        conn.execute(
            text(
                """
                INSERT INTO investigation_events (
                  id, investigation_id, sequence, type, public_payload
                ) VALUES (
                  CAST(:eid AS uuid), CAST(:iid AS uuid), :seq, :typ,
                  CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "eid": eid,
                "iid": iid,
                "seq": seq,
                "typ": typ,
                "payload": json.dumps(payload, ensure_ascii=False),
            },
        )
    row = {
        "id": eid,
        "investigation_id": iid,
        "sequence": seq,
        "type": typ,
        "public_payload": payload,
    }
    _maybe_wake(database_url=database_url, investigation_id=iid)
    return row


def list_events_after(
    *,
    database_url: str,
    investigation_id: str | UUID,
    last_event_id: int | str = 0,
) -> list[dict[str, Any]]:
    """Return events with sequence > last_event_id (SSE catch-up)."""
    try:
        after = int(last_event_id or 0)
    except (TypeError, ValueError):
        after = 0
    with _engine(database_url).connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id, sequence, type, public_payload
                FROM investigation_events
                WHERE investigation_id = CAST(:iid AS uuid)
                  AND sequence > :after
                ORDER BY sequence ASC
                """
            ),
            {"iid": str(investigation_id), "after": after},
        ).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = row["public_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        out.append(
            {
                "id": row["id"],
                "sequence": int(row["sequence"]),
                "type": row["type"],
                "public_payload": sanitize_public_payload(payload or {}),
            }
        )
    return out


list_after = list_events_after


def append_answer_chunk(
    *,
    database_url: str,
    investigation_id: str | UUID,
    chunk_text: str,
    validated: bool,
    result_status: str | None,
    evidence_ids: list[str] | None,
) -> dict[str, Any]:
    """Emit ANSWER_CHUNK only after claim/evidence validation (SPEC §4)."""
    if not validated:
        raise ValueError("unverified draft cannot be streamed as ANSWER_CHUNK")
    if not result_status:
        raise ValueError("ANSWER_CHUNK requires validated result_status")
    if not evidence_ids:
        raise ValueError("ANSWER_CHUNK requires evidence_ids")
    text_value = (chunk_text or "").strip()
    if not text_value:
        raise ValueError("empty chunk")

    payload = sanitize_public_payload(
        {
            "chunk": text_value,
            "result_status": result_status,
            "evidence_count": len(evidence_ids),
        }
    )
    # Bypass ANSWER_CHUNK guard in append_event by inserting directly with next seq.
    iid = str(investigation_id)
    with _engine(database_url).begin() as conn:
        nxt = conn.execute(
            text(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM investigation_events
                WHERE investigation_id = CAST(:iid AS uuid)
                """
            ),
            {"iid": iid},
        ).scalar_one()
        seq = int(nxt)
        eid = str(uuid4())
        conn.execute(
            text(
                """
                INSERT INTO investigation_events (
                  id, investigation_id, sequence, type, public_payload
                ) VALUES (
                  CAST(:eid AS uuid), CAST(:iid AS uuid), :seq, 'ANSWER_CHUNK',
                  CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "eid": eid,
                "iid": iid,
                "seq": seq,
                "payload": json.dumps(payload, ensure_ascii=False),
            },
        )
    row = {
        "id": eid,
        "investigation_id": iid,
        "sequence": seq,
        "type": "ANSWER_CHUNK",
        "event_type": "ANSWER_CHUNK",
        "public_payload": payload,
    }
    _maybe_wake(database_url=database_url, investigation_id=iid)
    return row


emit_answer_chunk = append_answer_chunk


def is_stale_last_event_id(
    *,
    database_url: str,
    investigation_id: str | UUID,
    last_event_id: int | str,
) -> bool:
    try:
        last = int(last_event_id)
    except (TypeError, ValueError):
        return True
    if last <= 0:
        return False
    return last > max_sequence(database_url=database_url, investigation_id=investigation_id)


def format_sse_frame(*, sequence: int, event_type: str, data: dict[str, Any]) -> str:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {sequence}\nevent: {event_type}\ndata: {body}\n\n"


def _maybe_wake(*, database_url: str, investigation_id: str) -> None:
    """Best-effort Redis wake for new events — PG remains the source of truth."""
    try:
        from support_platform.config import load_settings
        from support_platform.task_runtime.redis_adapter import RedisWakeAdapter

        settings = load_settings()
        redis_url = (getattr(settings, "redis_url", None) or "").strip()
        if not redis_url:
            return
        RedisWakeAdapter(redis_url=redis_url).enqueue_wake(investigation_id=investigation_id)
    except Exception:  # noqa: BLE001
        return
