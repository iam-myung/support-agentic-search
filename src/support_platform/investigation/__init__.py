"""Investigation domain API: run graph + fail stale RUNNING."""

from __future__ import annotations

from typing import Any

from support_platform.investigation.graph import build_investigation_graph

_FORBIDDEN = frozenset({"prompt", "system", "cot", "api_key", "internal"})


def _sanitize_step(step: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in step.items()
        if not str(k).startswith("_") and k not in _FORBIDDEN
    }


def run_investigation(
    *,
    question: str,
    retrieve: Any,
    chat: Any,
    store: Any,
    investigation_id: str | None = None,
) -> dict[str, Any]:
    """Run the investigation graph.

    When ``investigation_id`` is set (Phase 2 worker), reuse the existing row
    instead of inserting a new RUNNING investigation.
    """
    if investigation_id is None:
        iid = store.create_running(question=question)
    else:
        iid = str(investigation_id)
    graph = build_investigation_graph(retrieve=retrieve, chat=chat)
    final = graph.invoke(
        {
            "question": question,
            "investigation_id": iid,
            "public_steps": [],
            "retrieve_count": 0,
            "task_status": "RUNNING",
        }
    )
    row = store.get(iid)
    steps = [_sanitize_step(dict(step)) for step in list(final.get("public_steps") or [])]
    row["public_steps"] = steps
    row["task_status"] = final.get("task_status") or "COMPLETED"
    row["result_status"] = final.get("result_status")
    row["stopped_reason"] = final.get("stopped_reason")
    row["error_code"] = final.get("error_code")
    row["interrupt_id"] = final.get("interrupt_id")
    if not row.get("interrupt_id"):
        for step in steps:
            if step.get("interrupt_id"):
                row["interrupt_id"] = step["interrupt_id"]
                break
    summary = str(final.get("summary") or "").strip()
    if not summary:
        nested = final.get("output") if isinstance(final.get("output"), dict) else {}
        summary = str((nested or {}).get("summary") or "").strip()
    row["output"] = {
        "summary": summary,
        "hit_chunk_ids": [
            str(getattr(h, "chunk_id", h)) for h in list(final.get("hits") or [])
        ],
    }
    store.save(row)
    return store.get(iid)


def fail_stale_running(store: Any) -> list[str]:
    changed: list[str] = []
    for row in store.list_running():
        updated = dict(row)
        updated["task_status"] = "FAILED"
        updated["error_code"] = "PROCESS_INTERRUPTED"
        updated["result_status"] = None
        if not isinstance(updated.get("public_steps"), list):
            updated["public_steps"] = []
        store.save(updated)
        changed.append(str(updated["id"]))
    return changed
