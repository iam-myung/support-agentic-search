"""PostgreSQL LangGraph checkpointer adapter (SPEC §5) — business repos do not write this."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


class PostgresCheckpointerAdapter:
    """Persist graph checkpoints in PostgreSQL keyed by stable thread_id."""

    def __init__(self, *, database_url: str) -> None:
        self._engine: Engine = create_engine(database_url, pool_pre_ping=True)

    def thread_id_for(self, investigation_id: UUID | str) -> str:
        return f"inv:{investigation_id}"

    def put_checkpoint(
        self,
        *,
        thread_id: str,
        checkpoint_id: str,
        payload: dict[str, Any],
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO graph_checkpoints (thread_id, checkpoint_id, payload)
                    VALUES (:tid, :cid, CAST(:payload AS jsonb))
                    ON CONFLICT (thread_id) DO UPDATE SET
                      checkpoint_id = EXCLUDED.checkpoint_id,
                      payload = EXCLUDED.payload,
                      updated_at = NOW()
                    """
                ),
                {
                    "tid": thread_id,
                    "cid": checkpoint_id,
                    "payload": json.dumps(payload, ensure_ascii=False),
                },
            )

    def get_checkpoint(self, *, thread_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT checkpoint_id, payload
                    FROM graph_checkpoints
                    WHERE thread_id = :tid
                    """
                ),
                {"tid": thread_id},
            ).mappings().first()
        if row is None:
            return None
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return {"checkpoint_id": row["checkpoint_id"], "payload": payload or {}}
