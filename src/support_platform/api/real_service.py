"""Thin real InvestigationService adapter — reuses run_investigation (no second graph)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, text

from support_platform.api.investigations import (
    InvestigationDependencyError,
    NoAvailableKnowledgeError,
)
from support_platform.investigation import run_investigation


class RealInvestigationService:
    """OP-01～03 adapter over existing investigation + PG store + retrieve + chat ports."""

    def __init__(
        self,
        *,
        database_url: str,
        store: Any,
        retrieve: Any,
        chat: Any,
    ) -> None:
        self._database_url = database_url
        self._store = store
        self._retrieve = retrieve
        self._chat = chat
        self._engine = create_engine(database_url, pool_pre_ping=True)

    def _has_ready_knowledge(self) -> bool:
        with self._engine.connect() as conn:
            n = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM knowledge_versions kv
                    JOIN knowledge_sources ks ON ks.id = kv.source_id
                    WHERE kv.status = 'READY' AND kv.is_active = true
                      AND ks.status = 'ACTIVE'
                    """
                )
            ).scalar_one()
        return int(n or 0) > 0

    def create_investigation(
        self, *, question: str, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        _ = context
        if not self._has_ready_knowledge():
            raise NoAvailableKnowledgeError("no READY knowledge")
        try:
            row = run_investigation(
                question=question,
                retrieve=self._retrieve,
                chat=self._chat,
                store=self._store,
            )
        except Exception as exc:  # noqa: BLE001 - map dependency failures after create
            # Best-effort: find newest RUNNING/FAILED for this question context.
            running = list(self._store.list_running())
            if running:
                rid = str(running[-1]["id"])
                steps = list(running[-1].get("public_steps") or [])
                raise InvestigationDependencyError(
                    investigation_id=rid,
                    error_code="DEPENDENCY_ERROR",
                    public_steps=steps,
                ) from exc
            raise
        return {
            "id": row["id"],
            "task_status": row["task_status"],
            "result_status": row.get("result_status"),
            "public_steps": list(row.get("public_steps") or []),
            "claims": list(row.get("claims") or []),
            "evidence": list(row.get("evidence") or []),
            "output": row.get("output"),
            "error_code": row.get("error_code"),
        }

    def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
        try:
            row = self._store.get(investigation_id)
        except Exception:  # noqa: BLE001
            return None
        if not row:
            return None
        return {
            "id": row["id"],
            "task_status": row["task_status"],
            "result_status": row.get("result_status"),
            "public_steps": list(row.get("public_steps") or []),
            "claims": list(row.get("claims") or []),
            "evidence": list(row.get("evidence") or []),
            "output": row.get("output"),
            "error_code": row.get("error_code"),
        }

    def get_evidence(
        self, investigation_id: str, evidence_id: str
    ) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id::text AS id, investigation_id::text AS investigation_id,
                           claim_index, chunk_id::text AS chunk_id, relation,
                           quote_snapshot, locator_snapshot,
                           source_title_snapshot, version_snapshot
                    FROM investigation_evidence
                    WHERE id = CAST(:eid AS uuid)
                      AND investigation_id = CAST(:iid AS uuid)
                    """
                ),
                {"eid": evidence_id, "iid": investigation_id},
            ).mappings().first()
        if row is None:
            return None
        return dict(row)
