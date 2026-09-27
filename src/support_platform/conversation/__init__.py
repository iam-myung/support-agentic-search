"""Phase4 conversation boundary — wraps the agent tool loop for one turn."""

from __future__ import annotations

from typing import Any

from support_platform.investigation.agent_loop import run_phase4_agent_answer


def run_conversation_turn(
    *,
    content: str,
    retrieve: Any,
    web_search: Any,
    planner: Any,
    store: Any,
) -> dict[str, Any]:
    """Accept one user message and return assistant turn with knowledge_basis."""
    result = run_phase4_agent_answer(
        question=content,
        retrieve=retrieve,
        web_search=web_search,
        planner=planner,
        store=store,
    )
    output = dict(result.get("output") or {})
    knowledge_basis = output.get("knowledge_basis")
    return {
        "investigation_id": result.get("id"),
        "task_status": result.get("task_status"),
        "result_status": result.get("result_status"),
        "public_steps": list(result.get("public_steps") or []),
        "output": output,
        "knowledge_basis": knowledge_basis,
        "error_code": result.get("error_code"),
    }
