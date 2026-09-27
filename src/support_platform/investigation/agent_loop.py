"""Phase4 agent tool loop (REQ-016 / AC-020～022).

Whitelist: local_retrieve, web_search. Hard caps: tool≤8, web≤5.
Public steps never carry CoT / prompt / api_key.
"""

from __future__ import annotations

from typing import Any

from support_platform.infrastructure.web_search import build_public_web_search_step

MAX_TOOL_CALLS = 8
MAX_WEB_SEARCH_CALLS = 5
ALLOWED_TOOLS = frozenset({"local_retrieve", "web_search"})

_FORBIDDEN_PUBLIC = frozenset({"prompt", "system", "cot", "api_key", "internal"})
_FORBIDDEN_OUTPUT = frozenset({"prompt", "system", "cot", "api_key", "internal"})


def _public_step(kind: str, *, label: str, **fields: Any) -> dict[str, Any]:
    step: dict[str, Any] = {"kind": kind, "label": label}
    for key, value in fields.items():
        if key in _FORBIDDEN_PUBLIC:
            continue
        step[key] = value
    return step


def _sanitize_output(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in _FORBIDDEN_OUTPUT}


def _hit_ids(hits: list[Any]) -> list[str]:
    return [str(getattr(h, "chunk_id", h)) for h in hits]


def run_phase4_agent_answer(
    *,
    question: str,
    retrieve: Any,
    web_search: Any,
    planner: Any,
    store: Any,
    investigation_id: str | None = None,
) -> dict[str, Any]:
    """Run one Phase4 answer: understand → plan → tool loop → synthesize."""
    if investigation_id is None:
        iid = store.create_running(question=question)
    else:
        iid = str(investigation_id)

    row = store.get(iid)
    stripped = (question or "").strip()
    if not stripped:
        row["task_status"] = "FAILED"
        row["error_code"] = "EMPTY_QUESTION"
        row["result_status"] = None
        row["public_steps"] = []
        row["output"] = {"summary": "", "knowledge_basis": "LOCAL", "claims": []}
        store.save(row)
        return store.get(iid)

    public_steps: list[dict[str, Any]] = [
        _public_step("understand", label="理解目标", question_preview=stripped[:80]),
        _public_step(
            "plan",
            label="规划步骤",
            outline="依次：本地知识检索 → 网络检索（如需要）→ 生成建议与来源披露",
        ),
    ]
    tool_count = 0
    web_count = 0
    local_hits: list[Any] = []
    web_results: list[Any] = []
    stopped_reason: str | None = None
    hit_limit = False

    state: dict[str, Any] = {
        "question": stripped,
        "investigation_id": iid,
        "public_steps": public_steps,
        "tool_count": 0,
        "web_count": 0,
        "local_hits": local_hits,
        "web_results": web_results,
    }

    while True:
        if tool_count >= MAX_TOOL_CALLS:
            hit_limit = True
            stopped_reason = "tool_call_limit"
            break

        action = dict(planner.next_action(state) or {})
        atype = str(action.get("type") or "")

        if atype == "synthesize":
            break

        if atype != "tool":
            # Unknown plan action → stop safely without pretending success.
            stopped_reason = "invalid_plan_action"
            break

        tool = str(action.get("tool") or "")
        query = str(action.get("query") or stripped)

        if tool not in ALLOWED_TOOLS:
            public_steps.append(
                _public_step(
                    "tool_rejected",
                    label="拒绝未授权工具",
                    tool=tool,
                )
            )
            state["public_steps"] = public_steps
            continue

        if tool == "web_search" and web_count >= MAX_WEB_SEARCH_CALLS:
            hit_limit = True
            stopped_reason = "web_search_limit"
            break

        if tool == "local_retrieve":
            hits = list(retrieve(query))
            local_hits.extend(hits)
            tool_count += 1
            public_steps.append(
                _public_step(
                    "local_retrieve",
                    label="本地知识检索",
                    query=query,
                    hit_count=len(hits),
                    chunk_ids=_hit_ids(hits),
                )
            )
        elif tool == "web_search":
            result = web_search.search(query=query, max_results=5)
            web_results.append(result)
            tool_count += 1
            web_count += 1
            step = build_public_web_search_step(result)
            # Ensure Chinese label / kind contract even if builder changes.
            step = _public_step(
                "web_search",
                label=str(step.get("label") or "网络检索"),
                query=step.get("query", query),
                sources=step.get("sources") or [],
            )
            public_steps.append(step)

        state.update(
            {
                "public_steps": public_steps,
                "tool_count": tool_count,
                "web_count": web_count,
                "local_hits": local_hits,
                "web_results": web_results,
            }
        )

    if hit_limit:
        limit_label = (
            "达到网络检索上限，停止调查"
            if stopped_reason == "web_search_limit"
            else "达到工具调用上限，停止调查"
        )
        public_steps.append(_public_step("stop", label=limit_label))
        if stopped_reason == "web_search_limit":
            summary = "已达网络检索上限，尚未形成可确认结论；以下内容非基于本地知识库。"
            knowledge_basis = "WEB" if web_count and not local_hits else ("MIXED" if local_hits else "WEB")
        else:
            summary = "已达工具调用上限，尚未形成可确认结论。"
            knowledge_basis = "LOCAL" if local_hits and not web_count else (
                "WEB" if web_count and not local_hits else ("MIXED" if web_count else "LOCAL")
            )
        output = {
            "summary": summary,
            "knowledge_basis": knowledge_basis,
            "claims": [],
        }
        row["task_status"] = "COMPLETED"
        row["result_status"] = "NO_EVIDENCE"
        row["stopped_reason"] = stopped_reason
        row["error_code"] = None
        row["public_steps"] = public_steps
        row["output"] = output
        store.save(row)
        return store.get(iid)

    # Normal synthesize path
    raw = dict(planner.synthesize(state) or {})
    cleaned = _sanitize_output(raw)
    result_status = str(cleaned.get("result_status") or "NO_EVIDENCE")
    summary = str(cleaned.get("summary") or "").strip()
    knowledge_basis = str(cleaned.get("knowledge_basis") or "LOCAL").upper()
    if knowledge_basis not in {"LOCAL", "WEB", "MIXED"}:
        knowledge_basis = "LOCAL"
    claims_raw = cleaned.get("claims") or []
    claims: list[dict[str, Any]] = []
    for item in claims_raw:
        if not isinstance(item, dict):
            continue
        prov = str(item.get("provenance") or "LOCAL").upper()
        if prov not in {"LOCAL", "WEB"}:
            prov = "LOCAL"
        claims.append({"text": str(item.get("text") or ""), "provenance": prov})

    public_steps.append(
        _public_step(
            "synthesize",
            label="生成建议",
            result_status=result_status,
            knowledge_basis=knowledge_basis,
        )
    )

    row["task_status"] = "COMPLETED"
    row["result_status"] = result_status
    row["stopped_reason"] = stopped_reason
    row["error_code"] = None
    row["public_steps"] = public_steps
    row["output"] = {
        "summary": summary,
        "knowledge_basis": knowledge_basis,
        "claims": claims,
    }
    store.save(row)
    return store.get(iid)
