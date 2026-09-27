"""Freeze knowledge + Prompt/model/retrieval config versions at investigation create."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4
from pathlib import Path


def freeze_for_create(*, knowledge: Any, config: Any) -> dict[str, Any]:
    """Capture current active knowledge + config versions and content hashes."""
    active_ids = [str(v) for v in knowledge.list_investigation_candidates()]
    prompt = config.get_active(kind="prompt")
    model = config.get_active(kind="model")
    retrieval = config.get_active(kind="retrieval")
    if prompt is None or model is None or retrieval is None:
        raise ValueError("freeze requires active prompt, model, and retrieval config versions")

    knowledge_snapshot_hash = hashlib.sha256(
        json.dumps(sorted(active_ids), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    config_hash = hashlib.sha256(
        json.dumps(
            {
                "prompt": prompt["content_hash"],
                "model": model["content_hash"],
                "retrieval": retrieval["content_hash"],
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    return {
        "active_version_ids": active_ids,
        "prompt_version": str(prompt["version_id"]),
        "model_config_version": str(model["version_id"]),
        "retrieval_config_version": str(retrieval["version_id"]),
        "knowledge_snapshot_hash": knowledge_snapshot_hash,
        "config_hash": config_hash,
    }


class PgKnowledgeCandidates:
    """Read active READY knowledge version IDs from PostgreSQL."""

    def __init__(self, database_url: str) -> None:
        from sqlalchemy import create_engine

        self._engine = create_engine(database_url, pool_pre_ping=True)

    def list_investigation_candidates(self) -> list[str]:
        from sqlalchemy import text

        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT kv.id::text AS id
                    FROM knowledge_versions kv
                    JOIN knowledge_sources ks ON ks.id = kv.source_id
                    WHERE kv.is_active = true
                      AND kv.status = 'READY'
                      AND ks.status = 'ACTIVE'
                    ORDER BY kv.updated_at ASC
                    """
                )
            ).mappings().all()
        return [r["id"] for r in rows]


def create_pg_investigation_with_snapshot(
    *,
    database_url: str,
    question: str,
    config_root: str | Path,
) -> dict[str, Any]:
    """Official OP-00 persist path: freeze actives into real PG (no LLM required)."""
    from pathlib import Path as _Path

    from support_platform.config_mgmt.pg_store import PgConfigVersionService
    from support_platform.investigation.pg_store import PgInvestigationStore

    config = PgConfigVersionService(
        database_url=database_url,
        config_root=_Path(config_root),
    )
    knowledge = PgKnowledgeCandidates(database_url)
    frozen = freeze_for_create(knowledge=knowledge, config=config)
    store = PgInvestigationStore(database_url)
    iid = store.create_running(question=question, frozen=frozen)
    row = store.get(iid)
    row["task_status"] = "COMPLETED"
    row["result_status"] = "NEEDS_HUMAN"
    row["public_steps"] = [{"name": "freeze_snapshot", "status": "ok"}]
    store.save(row)
    return {"id": iid, "store": store, "frozen": frozen}


class _MemorySnapshotStore:
    """Minimal store that persists frozen snapshot fields (unit / GREEN path)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def create_running(self, *, question: str, frozen: dict[str, Any]) -> str:
        iid = str(uuid4())
        self.rows[iid] = {
            "id": iid,
            "question": question,
            "task_status": "RUNNING",
            "result_status": None,
            "public_steps": [],
            "error_code": None,
            "output": None,
            "active_version_ids": list(frozen["active_version_ids"]),
            "prompt_version": str(frozen["prompt_version"]),
            "model_config_version": str(frozen["model_config_version"]),
            "retrieval_config_version": str(frozen["retrieval_config_version"]),
            "knowledge_snapshot_hash": str(frozen["knowledge_snapshot_hash"]),
            "config_hash": str(frozen["config_hash"]),
        }
        return iid

    def get(self, investigation_id: str) -> dict[str, Any]:
        return dict(self.rows[investigation_id])


def create_with_snapshot(
    *,
    question: str,
    knowledge: Any,
    config: Any,
) -> dict[str, Any]:
    """Create an investigation row with frozen versions; mark configs referenced."""
    frozen = freeze_for_create(knowledge=knowledge, config=config)
    store = _MemorySnapshotStore()
    iid = store.create_running(question=question, frozen=frozen)
    for key in ("prompt_version", "model_config_version", "retrieval_config_version"):
        config.mark_referenced(frozen[key], investigation_id=iid)
    return {"id": iid, "store": store, "frozen": frozen}


def get_frozen(store: Any, investigation_id: str) -> dict[str, Any]:
    """Return frozen snapshot fields from a stored investigation (never re-read actives)."""
    row = store.get(investigation_id)
    return {
        "active_version_ids": list(row["active_version_ids"]),
        "prompt_version": str(row["prompt_version"]),
        "model_config_version": str(row["model_config_version"]),
        "retrieval_config_version": str(row["retrieval_config_version"]),
        "knowledge_snapshot_hash": str(row["knowledge_snapshot_hash"]),
        "config_hash": str(row["config_hash"]),
    }


def replay_versions(store: Any, investigation_id: str) -> dict[str, Any]:
    """OP-00 style replay: versions come only from the frozen investigation row."""
    return get_frozen(store, investigation_id)
