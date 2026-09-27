"""Task status + final-output ownership backed by PostgreSQL (SPEC §3)."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError


def _engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def _json_load(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def create_queued_investigation(
    *,
    database_url: str,
    investigation_id: UUID | str,
    question: str,
    context: dict[str, Any] | None,
    owner_user_id: UUID | str,
    knowledge_version_ids: list[Any] | None = None,
    config_versions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a new investigation in QUEUED state (OP-07 fact write)."""
    iid = UUID(str(investigation_id))
    owner = UUID(str(owner_user_id))
    avs = list(knowledge_version_ids or [])
    cfg = config_versions or {}
    with _engine(database_url).begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO investigations (
                  id, question, context, active_version_ids, task_status,
                  result_status, output, public_steps, error_code, config_hash,
                  knowledge_snapshot_hash, prompt_version,
                  model_config_version, retrieval_config_version, owner_user_id
                ) VALUES (
                  CAST(:id AS uuid), :q, CAST(:ctx AS jsonb), CAST(:avs AS jsonb),
                  'QUEUED', NULL, NULL, CAST(:steps AS jsonb), NULL, :ch,
                  :ksh, :pv, :mv, :rv, CAST(:owner AS uuid)
                )
                """
            ),
            {
                "id": str(iid),
                "q": question,
                "ctx": json.dumps(context or {}, ensure_ascii=False),
                "avs": json.dumps(avs, ensure_ascii=False),
                "steps": "[]",
                "ch": cfg.get("config_hash"),
                "ksh": cfg.get("knowledge_snapshot_hash"),
                "pv": cfg.get("prompt_version"),
                "mv": cfg.get("model_config_version"),
                "rv": cfg.get("retrieval_config_version"),
                "owner": str(owner),
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO investigation_events (
                  id, investigation_id, sequence, type, public_payload
                ) VALUES (
                  CAST(:eid AS uuid), CAST(:iid AS uuid), 1, 'QUEUED',
                  CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "eid": str(uuid4()),
                "iid": str(iid),
                "payload": json.dumps({"task_status": "QUEUED"}, ensure_ascii=False),
            },
        )
    return {"id": str(iid), "task_status": "QUEUED"}


def mark_running(*, database_url: str, investigation_id: UUID | str) -> None:
    with _engine(database_url).begin() as conn:
        conn.execute(
            text(
                """
                UPDATE investigations
                SET task_status = 'RUNNING'
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": str(investigation_id)},
        )


def get_task(*, database_url: str, investigation_id: UUID | str) -> dict[str, Any] | None:
    with _engine(database_url).connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id, question, context, task_status, result_status,
                       output, public_steps, error_code, owner_user_id::text AS owner_user_id,
                       knowledge_snapshot_hash, prompt_version,
                       model_config_version, retrieval_config_version,
                       active_version_ids
                FROM investigations
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": str(investigation_id)},
        ).mappings().first()
    if row is None:
        return None
    return {
        "id": row["id"],
        "question": row["question"],
        "context": _json_load(row["context"]) or {},
        "task_status": row["task_status"],
        "result_status": row["result_status"],
        "output": _json_load(row["output"]),
        "public_steps": _json_load(row["public_steps"]) or [],
        "error_code": row["error_code"],
        "owner_user_id": row["owner_user_id"],
        "knowledge_snapshot_hash": row["knowledge_snapshot_hash"],
        "prompt_version": row["prompt_version"],
        "model_config_version": row["model_config_version"],
        "retrieval_config_version": row["retrieval_config_version"],
        "active_version_ids": _json_load(row["active_version_ids"]) or [],
    }


def persist_final_output(
    *,
    database_url: str,
    investigation_id: UUID | str,
    idempotency_key: str,
    result_status: str,
    output: dict[str, Any],
    public_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write final output once; unique (investigation_id, idempotency_key) blocks duplicates."""
    iid = str(investigation_id)
    steps = public_steps or []
    payload = {"result_status": result_status, "output": output, "public_steps": steps}
    engine = _engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO node_executions (
                      id, investigation_id, idempotency_key, node_name, status, result_payload
                    ) VALUES (
                      CAST(:nid AS uuid), CAST(:iid AS uuid), :ikey, 'final_output',
                      'COMPLETED', CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "nid": str(uuid4()),
                    "iid": iid,
                    "ikey": idempotency_key,
                    "payload": json.dumps(payload, ensure_ascii=False),
                },
            )
            conn.execute(
                text(
                    """
                    UPDATE investigations SET
                      task_status = 'COMPLETED',
                      result_status = :rs,
                      output = CAST(:out AS jsonb),
                      public_steps = CAST(:steps AS jsonb)
                    WHERE id = CAST(:iid AS uuid)
                    """
                ),
                {
                    "iid": iid,
                    "rs": result_status,
                    "out": json.dumps(output, ensure_ascii=False),
                    "steps": json.dumps(steps, ensure_ascii=False),
                },
            )
        return {
            "accepted": True,
            "duplicate": False,
            "task_status": "COMPLETED",
            "result_status": result_status,
            "output": output,
            "public_steps": steps,
        }
    except IntegrityError:
        existing = get_task(database_url=database_url, investigation_id=iid)
        out = (existing or {}).get("output") or {}
        return {
            "accepted": False,
            "duplicate": True,
            "task_status": (existing or {}).get("task_status") or "COMPLETED",
            "result_status": (existing or {}).get("result_status"),
            "output": out,
            "public_steps": (existing or {}).get("public_steps") or [],
        }


def count_final_outputs(*, database_url: str, investigation_id: UUID | str) -> int:
    with _engine(database_url).connect() as conn:
        n = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM node_executions
                WHERE investigation_id = CAST(:iid AS uuid)
                  AND node_name = 'final_output'
                  AND status = 'COMPLETED'
                """
            ),
            {"iid": str(investigation_id)},
        ).scalar_one()
    return int(n)


def body_hash_for_create(*, question: str, context: dict[str, Any] | None) -> str:
    raw = json.dumps(
        {"question": question, "context": context or {}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lookup_create_idempotency(
    *,
    database_url: str,
    owner_user_id: UUID | str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    with _engine(database_url).connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT body_hash, investigation_id::text AS investigation_id
                FROM investigation_create_keys
                WHERE owner_user_id = CAST(:owner AS uuid)
                  AND idempotency_key = :ikey
                """
            ),
            {"owner": str(owner_user_id), "ikey": idempotency_key},
        ).mappings().first()
    if row is None:
        return None
    return {"body_hash": row["body_hash"], "investigation_id": row["investigation_id"]}


def remember_create_idempotency(
    *,
    database_url: str,
    owner_user_id: UUID | str,
    idempotency_key: str,
    body_hash: str,
    investigation_id: UUID | str,
) -> None:
    with _engine(database_url).begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO investigation_create_keys (
                  id, owner_user_id, idempotency_key, body_hash, investigation_id
                ) VALUES (
                  CAST(:id AS uuid), CAST(:owner AS uuid), :ikey, :bh, CAST(:iid AS uuid)
                )
                """
            ),
            {
                "id": str(uuid4()),
                "owner": str(owner_user_id),
                "ikey": idempotency_key,
                "bh": body_hash,
                "iid": str(investigation_id),
            },
        )
