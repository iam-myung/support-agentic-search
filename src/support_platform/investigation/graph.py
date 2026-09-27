"""LangGraph investigation StateGraph — Phase3: ≤3 retrieve rounds + public labels."""

from __future__ import annotations

import hashlib
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

MAX_RETRIEVE_ROUNDS = 3
_FORBIDDEN_PUBLIC = frozenset({"prompt", "system", "cot", "api_key", "internal"})


class InvestigationState(TypedDict, total=False):
    question: str
    investigation_id: str
    constraints: dict[str, Any]
    query: str
    hits: list[Any]
    prev_chunk_ids: list[str]
    public_steps: list[dict[str, Any]]
    retrieve_count: int
    assessment: dict[str, Any]
    task_status: str
    result_status: str | None
    stopped_reason: str | None
    error_code: str | None
    new_chunk_ids: list[str]
    interrupt_id: str | None
    summary: str
    output: dict[str, Any]


RetrieveFn = Callable[..., list[Any]]
ChatPort = Any


def _hit_ids(hits: list[Any]) -> list[str]:
    return [str(getattr(h, "chunk_id", h)) for h in hits]


def _public_step(kind: str, *, label: str, **fields: Any) -> dict[str, Any]:
    """Public process step: kind + Chinese label; never attach CoT/prompt/secrets."""
    step: dict[str, Any] = {"kind": kind, "label": label}
    for key, value in fields.items():
        if key in _FORBIDDEN_PUBLIC:
            continue
        step[key] = value
    return step


def _sanitize_assessment(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if k not in _FORBIDDEN_PUBLIC}


def build_investigation_graph(*, retrieve: RetrieveFn, chat: ChatPort) -> Any:
    def validate(state: InvestigationState) -> dict[str, Any]:
        question = (state.get("question") or "").strip()
        if not question:
            return {
                "task_status": "FAILED",
                "error_code": "EMPTY_QUESTION",
                "result_status": None,
            }
        return {"task_status": "RUNNING", "error_code": None}

    def understand_goal(state: InvestigationState) -> dict[str, Any]:
        if state.get("task_status") == "FAILED":
            return {}
        question = state["question"]
        steps = list(state.get("public_steps") or [])
        steps.append(
            _public_step(
                "understand_goal",
                label="理解目标",
                question_preview=question[:80],
            )
        )
        return {
            "constraints": {"question": question},
            "public_steps": steps,
            "retrieve_count": 0,
            "prev_chunk_ids": [],
            "stopped_reason": None,
            "result_status": None,
            "summary": "",
        }

    def plan_subqueries(state: InvestigationState) -> dict[str, Any]:
        if state.get("task_status") == "FAILED":
            return {}
        question = state["question"]
        steps = list(state.get("public_steps") or [])
        steps.append(
            _public_step(
                "plan_subqueries",
                label="拆分子查询",
            )
        )
        return {
            "query": question,
            "public_steps": steps,
        }

    def retrieve_round(state: InvestigationState) -> dict[str, Any]:
        if state.get("task_status") == "FAILED":
            return {}
        query = state.get("query") or state["question"]
        hits = list(retrieve(query))
        count = int(state.get("retrieve_count") or 0) + 1
        prev = set(state.get("prev_chunk_ids") or [])
        ids = _hit_ids(hits)
        new_ids = [cid for cid in ids if cid not in prev]
        steps = list(state.get("public_steps") or [])
        steps.append(
            _public_step(
                "retrieve",
                label=f"检索 {count}/{MAX_RETRIEVE_ROUNDS}",
                hit_count=len(hits),
                chunk_ids=ids,
                round=count,
            )
        )
        return {
            "hits": hits,
            "retrieve_count": count,
            "prev_chunk_ids": ids,
            "new_chunk_ids": new_ids,
            "public_steps": steps,
        }

    def compare_evidence(state: InvestigationState) -> dict[str, Any]:
        if state.get("task_status") == "FAILED":
            return {}
        # After a follow-up retrieve, no new chunks → stop without another assess loop.
        if int(state.get("retrieve_count") or 0) > 1 and not list(state.get("new_chunk_ids") or []):
            steps = list(state.get("public_steps") or [])
            steps.append(
                _public_step(
                    "compare_evidence",
                    label="比较证据",
                    note="no_new_evidence",
                )
            )
            return {
                "assessment": {"next_action": "no_new_evidence"},
                "public_steps": steps,
                "result_status": "NO_EVIDENCE",
                "stopped_reason": "no_new_evidence",
            }
        raw = chat.assess(
            question=state["question"],
            hits=list(state.get("hits") or []),
            public_only=True,
        )
        assessment = _sanitize_assessment(dict(raw or {}))
        steps = list(state.get("public_steps") or [])
        steps.append(
            _public_step(
                "compare_evidence",
                label="比较证据",
                next_action=assessment.get("next_action"),
            )
        )
        return {"assessment": assessment, "public_steps": steps}

    def route_after_compare(state: InvestigationState) -> str:
        if state.get("task_status") == "FAILED":
            return "finish"
        action = (state.get("assessment") or {}).get("next_action")
        count = int(state.get("retrieve_count") or 0)
        if action == "no_new_evidence":
            return "stop_no_new"
        if action == "sufficient":
            return "synthesize"
        if action == "conflict":
            return "synthesize"
        if action == "no_evidence":
            return "synthesize"
        if action == "human_needed":
            return "terminal"
        if action == "resolvable_gap":
            if count < MAX_RETRIEVE_ROUNDS:
                return "rewrite"
            return "stop_limit"
        return "synthesize"

    def rewrite(state: InvestigationState) -> dict[str, Any]:
        gaps = list((state.get("assessment") or {}).get("gaps") or [])
        new_query = chat.rewrite(
            question=state["question"],
            constraints=dict(state.get("constraints") or {}),
            gaps=gaps,
        )
        steps = list(state.get("public_steps") or [])
        steps.append(
            _public_step(
                "plan_subqueries",
                label="信息不足，发起补查",
                gaps=gaps,
            )
        )
        return {"query": new_query, "public_steps": steps}

    def stop_no_new(state: InvestigationState) -> dict[str, Any]:
        return {
            "task_status": "COMPLETED",
            "stopped_reason": "no_new_evidence",
            "result_status": "NO_EVIDENCE",
            "summary": ((state.get("summary") or "") or "未找到新的可用证据。"),
        }

    def stop_limit(state: InvestigationState) -> dict[str, Any]:
        steps = list(state.get("public_steps") or [])
        steps.append(
            _public_step(
                "compare_evidence",
                label="达到检索轮次上限，停止调查",
            )
        )
        return {
            "task_status": "COMPLETED",
            "stopped_reason": "retrieve_round_limit",
            "result_status": "NO_EVIDENCE",
            "summary": "已达检索轮次上限，尚未形成可确认结论。",
            "public_steps": steps,
        }

    def synthesize(state: InvestigationState) -> dict[str, Any]:
        action = (state.get("assessment") or {}).get("next_action")
        status_map = {
            "sufficient": "ANSWERED",
            "conflict": "CONFLICT",
            "no_evidence": "NO_EVIDENCE",
            "human_needed": "NEEDS_HUMAN",
        }
        result_status = status_map.get(str(action), state.get("result_status") or "NO_EVIDENCE")
        summary = ""
        synth = getattr(chat, "synthesize", None)
        if callable(synth):
            raw = synth(
                question=state["question"],
                hits=list(state.get("hits") or []),
                assessment=_sanitize_assessment(dict(state.get("assessment") or {})),
                result_status=str(result_status),
            )
            payload = dict(raw or {})
            result_status = str(payload.get("result_status") or result_status)
            summary = str(payload.get("summary") or "").strip()
        if result_status in {"ANSWERED", "CONFLICT"} and not summary:
            # Contract requires non-empty; Fake must supply — leave empty so tests catch bugs.
            summary = ""
        if result_status in {"NO_EVIDENCE", "NEEDS_HUMAN"} and not summary:
            summary = "信息不足，需补充知识或转人工。" if result_status == "NO_EVIDENCE" else "需要人工确认后继续。"
        steps = list(state.get("public_steps") or [])
        steps.append(
            _public_step(
                "synthesize",
                label="生成建议",
                result_status=result_status,
            )
        )
        return {
            "task_status": "COMPLETED",
            "result_status": result_status,
            "summary": summary,
            "output": {"summary": summary},
            "public_steps": steps,
            "stopped_reason": state.get("stopped_reason"),
        }

    def terminal(state: InvestigationState) -> dict[str, Any]:
        inv = str(state.get("investigation_id") or state.get("question") or "anon")
        interrupt_id = state.get("interrupt_id") or (
            f"intr-{hashlib.sha256(f'{inv}:human_needed'.encode()).hexdigest()[:16]}"
        )
        steps = list(state.get("public_steps") or [])
        steps.append(
            _public_step(
                "review_required",
                label="等待人工确认",
                interrupt_id=interrupt_id,
                reason="human_needed",
            )
        )
        return {
            "task_status": "INTERRUPTED",
            "result_status": None,
            "stopped_reason": "human_needed",
            "interrupt_id": interrupt_id,
            "public_steps": steps,
            "summary": "需要人工补充或确认后继续。",
        }

    graph = StateGraph(InvestigationState)
    graph.add_node("validate", validate)
    graph.add_node("understand_goal", understand_goal)
    graph.add_node("plan_subqueries", plan_subqueries)
    graph.add_node("retrieve_round", retrieve_round)
    graph.add_node("compare_evidence", compare_evidence)
    graph.add_node("rewrite", rewrite)
    graph.add_node("stop_no_new", stop_no_new)
    graph.add_node("stop_limit", stop_limit)
    graph.add_node("synthesize", synthesize)
    graph.add_node("terminal", terminal)

    graph.set_entry_point("validate")
    graph.add_edge("validate", "understand_goal")
    graph.add_edge("understand_goal", "plan_subqueries")
    graph.add_edge("plan_subqueries", "retrieve_round")
    graph.add_edge("retrieve_round", "compare_evidence")
    graph.add_conditional_edges(
        "compare_evidence",
        route_after_compare,
        {
            "rewrite": "rewrite",
            "synthesize": "synthesize",
            "terminal": "terminal",
            "stop_limit": "stop_limit",
            "stop_no_new": "stop_no_new",
            "finish": END,
        },
    )
    graph.add_edge("rewrite", "retrieve_round")
    graph.add_edge("stop_no_new", END)
    graph.add_edge("stop_limit", END)
    graph.add_edge("synthesize", END)
    graph.add_edge("terminal", END)
    return graph.compile()
