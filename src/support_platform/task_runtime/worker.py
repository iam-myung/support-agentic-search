"""Worker processing helpers — lease + idempotent finals (SPEC §3/§8 S12)."""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text

from support_platform.task_runtime.service import (
    count_final_outputs,
    get_task,
    persist_final_output,
)


def _engine(database_url: str):
    return create_engine(database_url, pool_pre_ping=True)


def _mark_task_running(*, database_url: str, investigation_id: str) -> None:
    with _engine(database_url).begin() as conn:
        conn.execute(
            text(
                """
                UPDATE investigations
                SET task_status = 'RUNNING'
                WHERE id = CAST(:iid AS uuid)
                  AND task_status IN ('QUEUED', 'RUNNING')
                """
            ),
            {"iid": investigation_id},
        )


def default_worker_mode() -> str:
    """Production default is real investigation; tests may force deterministic_final."""
    raw = (os.environ.get("WORKER_MODE") or "real").strip().lower()
    if raw in {"real", "deterministic_final"}:
        return raw
    return "real"


def run_queued_investigation(
    *,
    database_url: str,
    investigation_id: UUID | str,
    question: str,
) -> dict[str, Any]:
    """Execute real Chat+Embedding+PG investigation for an existing queued task."""
    from support_platform.composition import build_retrieve, require_real_runtime_env
    from support_platform.infrastructure.chat import HttpChatAdapter
    from support_platform.infrastructure.embedding import HttpEmbeddingAdapter
    from support_platform.investigation import run_investigation
    from support_platform.investigation.pg_store import PgInvestigationStore

    env = {**require_real_runtime_env(), "DATABASE_URL": database_url}
    embedding = HttpEmbeddingAdapter(
        base_url=env["EMBEDDING_BASE_URL"],
        api_key=env["EMBEDDING_API_KEY"],
        model=env["EMBEDDING_MODEL"],
        dimension=int(env["EMBEDDING_DIMENSION"]),
    )
    chat = HttpChatAdapter(
        base_url=env["LLM_BASE_URL"],
        api_key=env["LLM_API_KEY"],
        model=env["LLM_MODEL"],
    )
    if type(chat).__name__ == "FakeChatAdapter":
        raise RuntimeError("Fake chat refused in worker real mode")
    store = PgInvestigationStore(database_url)
    retrieve = build_retrieve(env, embedding)
    return run_investigation(
        question=question,
        retrieve=retrieve,
        chat=chat,
        store=store,
        investigation_id=str(investigation_id),
    )


def process_investigation(
    *,
    database_url: str,
    investigation_id: UUID | str,
    worker_id: str = "worker",
    mode: str | None = None,
) -> dict[str, Any]:
    """Process one investigation.

    ``deterministic_final`` — test-only stub (no paid model).
    ``real`` — official path: Chat + Embedding + PG graph on the queued id.
    """
    resolved = (mode or default_worker_mode()).strip().lower()
    iid = str(investigation_id)

    _mark_task_running(database_url=database_url, investigation_id=iid)

    if resolved == "deterministic_final":
        return persist_final_output(
            database_url=database_url,
            investigation_id=iid,
            idempotency_key=f"final:{iid}",
            result_status="ANSWERED",
            output={"summary": f"deterministic:{worker_id}"},
            public_steps=[{"step": "worker_deterministic", "worker_id": worker_id}],
        )

    if resolved != "real":
        raise ValueError(f"unsupported worker mode: {resolved}")

    task = get_task(database_url=database_url, investigation_id=iid)
    if task is None:
        raise RuntimeError(f"investigation not found: {iid}")

    if count_final_outputs(database_url=database_url, investigation_id=iid) > 0:
        return {
            "accepted": False,
            "duplicate": True,
            "task_status": task.get("task_status") or "COMPLETED",
            "result_status": task.get("result_status"),
            "output": task.get("output") or {},
            "public_steps": task.get("public_steps") or [],
        }

    row = run_queued_investigation(
        database_url=database_url,
        investigation_id=iid,
        question=str(task.get("question") or ""),
    )
    status = row.get("task_status") or "COMPLETED"
    if status == "INTERRUPTED":
        return {
            "accepted": True,
            "duplicate": False,
            "task_status": "INTERRUPTED",
            "result_status": row.get("result_status"),
            "output": row.get("output") or {},
            "public_steps": list(row.get("public_steps") or []),
            "interrupt_id": row.get("interrupt_id"),
        }
    if status == "FAILED":
        return {
            "accepted": True,
            "duplicate": False,
            "task_status": "FAILED",
            "result_status": None,
            "output": row.get("output") or {},
            "public_steps": list(row.get("public_steps") or []),
            "error_code": row.get("error_code"),
        }

    result_status = str(row.get("result_status") or "NO_EVIDENCE")
    return persist_final_output(
        database_url=database_url,
        investigation_id=iid,
        idempotency_key=f"final:{iid}",
        result_status=result_status,
        output=row.get("output") or {},
        public_steps=list(row.get("public_steps") or []),
    )


def run_one(
    *,
    database_url: str,
    investigation_id: UUID | str,
    worker_id: str = "worker",
    mode: str | None = None,
) -> dict[str, Any]:
    return process_investigation(
        database_url=database_url,
        investigation_id=investigation_id,
        worker_id=worker_id,
        mode=mode,
    )


def mark_unsafe_resume_failed(
    *,
    database_url: str,
    investigation_id: UUID | str,
    error_code: str = "UNSAFE_RESUME",
    public_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Cannot safely continue → FAILED with retained public steps (AC-009)."""
    steps = public_steps or [{"step": "unsafe_resume"}]
    with _engine(database_url).begin() as conn:
        conn.execute(
            text(
                """
                UPDATE investigations SET
                  task_status = 'FAILED',
                  result_status = NULL,
                  error_code = :ec,
                  public_steps = CAST(:steps AS jsonb)
                WHERE id = CAST(:iid AS uuid)
                """
            ),
            {
                "iid": str(investigation_id),
                "ec": error_code,
                "steps": json.dumps(steps, ensure_ascii=False),
            },
        )
    return {
        "task_status": "FAILED",
        "error_code": error_code,
        "result_status": None,
        "public_steps": steps,
    }


def fail_unsafe_resume(
    *,
    database_url: str,
    investigation_id: UUID | str,
    error_code: str = "UNSAFE_RESUME",
    public_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return mark_unsafe_resume_failed(
        database_url=database_url,
        investigation_id=investigation_id,
        error_code=error_code,
        public_steps=public_steps,
    )
