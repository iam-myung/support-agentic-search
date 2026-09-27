"""Feedback append store — never mutates investigation AI output; persists to PG."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, text

_rows: list[dict[str, Any]] = []


def reset() -> None:
    _rows.clear()


def list_for(investigation_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in _rows if r.get("investigation_id") == investigation_id]


def _resolve_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    # store.py → feedback → support_platform → src → repo root
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == "DATABASE_URL":
                return value.strip().strip('"').strip("'")
    raise RuntimeError("DATABASE_URL is required to persist investigation feedback")


def append_feedback(
    *,
    investigation_id: str,
    action: str,
    reason: str | None = None,
    edited_text: str | None = None,
) -> dict[str, Any]:
    row = {
        "id": str(uuid4()),
        "investigation_id": investigation_id,
        "action": action,
        "reason": reason,
        "edited_text": edited_text,
    }
    engine = create_engine(_resolve_database_url(), pool_pre_ping=True)
    with engine.begin() as conn:
        # Ensure FK parent when InvestigationService validated the id in-memory only.
        conn.execute(
            text(
                """
                INSERT INTO investigations (
                  id, question, context, active_version_ids, task_status,
                  result_status, output, public_steps, error_code, config_hash
                ) VALUES (
                  CAST(:id AS uuid), :q, CAST(:ctx AS jsonb), CAST(:avs AS jsonb),
                  'COMPLETED', NULL, CAST(:out AS jsonb), CAST(:steps AS jsonb),
                  NULL, NULL
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": investigation_id,
                "q": "feedback-parent",
                "ctx": "{}",
                "avs": "[]",
                "out": "{}",
                "steps": "[]",
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO investigation_feedback (
                  id, investigation_id, action, reason, edited_text
                ) VALUES (
                  CAST(:id AS uuid), CAST(:iid AS uuid), :action, :reason, :edited_text
                )
                """
            ),
            {
                "id": row["id"],
                "iid": investigation_id,
                "action": action,
                "reason": reason,
                "edited_text": edited_text,
            },
        )
    _rows.append(row)
    return dict(row)
