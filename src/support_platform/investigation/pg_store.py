"""PostgreSQL-backed investigation store for Phase 1 SMOKE (no Checkpointer)."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


class PgInvestigationStore:
    def __init__(self, database_url: str) -> None:
        # Fail fast when Docker/Postgres is down (default OS timeout feels like "no response").
        self._engine: Engine = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
            pool_timeout=5,
        )

    def create_running(
        self,
        *,
        question: str,
        frozen: dict[str, Any] | None = None,
    ) -> str:
        iid = str(uuid4())
        avs = list((frozen or {}).get("active_version_ids") or [])
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO investigations (
                      id, question, context, active_version_ids, task_status,
                      result_status, output, public_steps, error_code, config_hash,
                      knowledge_snapshot_hash, prompt_version,
                      model_config_version, retrieval_config_version
                    ) VALUES (
                      CAST(:id AS uuid), :q, CAST(:ctx AS jsonb), CAST(:avs AS jsonb),
                      'RUNNING', NULL, NULL, CAST(:steps AS jsonb), NULL, :ch,
                      :ksh, :pv, :mv, :rv
                    )
                    """
                ),
                {
                    "id": iid,
                    "q": question,
                    "ctx": "{}",
                    "avs": json.dumps(avs, ensure_ascii=False),
                    "steps": "[]",
                    "ch": (frozen or {}).get("config_hash"),
                    "ksh": (frozen or {}).get("knowledge_snapshot_hash"),
                    "pv": (frozen or {}).get("prompt_version"),
                    "mv": (frozen or {}).get("model_config_version"),
                    "rv": (frozen or {}).get("retrieval_config_version"),
                },
            )
        return iid

    def save(self, row: dict[str, Any]) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE investigations SET
                      task_status = :ts,
                      result_status = :rs,
                      public_steps = CAST(:steps AS jsonb),
                      error_code = :ec,
                      output = CAST(:out AS jsonb)
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {
                    "id": row["id"],
                    "ts": row["task_status"],
                    "rs": row.get("result_status"),
                    "steps": json.dumps(row.get("public_steps") or [], ensure_ascii=False),
                    "ec": row.get("error_code"),
                    "out": json.dumps(row.get("output") or {}, ensure_ascii=False),
                },
            )

    def get(self, investigation_id: str) -> dict[str, Any]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id::text AS id, question, task_status, result_status,
                           public_steps, error_code, output,
                           active_version_ids, config_hash, knowledge_snapshot_hash,
                           prompt_version, model_config_version, retrieval_config_version
                    FROM investigations WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": investigation_id},
            ).mappings().one()
        steps = row["public_steps"]
        if isinstance(steps, str):
            steps = json.loads(steps)
        output = row["output"]
        if isinstance(output, str):
            output = json.loads(output)
        avs = row["active_version_ids"]
        if isinstance(avs, str):
            avs = json.loads(avs)
        return {
            "id": row["id"],
            "question": row["question"],
            "task_status": row["task_status"],
            "result_status": row["result_status"],
            "public_steps": steps or [],
            "error_code": row["error_code"],
            "output": output,
            "active_version_ids": avs or [],
            "config_hash": row["config_hash"],
            "knowledge_snapshot_hash": row["knowledge_snapshot_hash"],
            "prompt_version": row["prompt_version"],
            "model_config_version": row["model_config_version"],
            "retrieval_config_version": row["retrieval_config_version"],
        }

    def list_running(self) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id::text AS id, question, task_status, result_status,
                           public_steps, error_code, output
                    FROM investigations WHERE task_status = 'RUNNING'
                    """
                )
            ).mappings().all()
        out: list[dict[str, Any]] = []
        for row in rows:
            steps = row["public_steps"]
            if isinstance(steps, str):
                steps = json.loads(steps)
            output = row["output"]
            if isinstance(output, str):
                output = json.loads(output)
            out.append(
                {
                    "id": row["id"],
                    "question": row["question"],
                    "task_status": row["task_status"],
                    "result_status": row["result_status"],
                    "public_steps": steps or [],
                    "error_code": row["error_code"],
                    "output": output,
                }
            )
        return out
