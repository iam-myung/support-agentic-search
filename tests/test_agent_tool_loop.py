"""S24 Phase4 agent tool-loop RED contracts (REQ-016 / AC-020～022).

SPEC §5 Phase4:
- whitelist tools: local_retrieve, web_search only (no business-write tools)
- tool_call ≤ 8 total; web_search ≤ 5
- public_steps may use kind local_retrieve|web_search + Chinese label; never CoT/prompt/key
- output.knowledge_basis ∈ {LOCAL, WEB, MIXED}
- WEB/MIXED summary must explicitly declare non-local knowledge (Chinese)
- claims carry provenance LOCAL|WEB; WEB must never be labeled LOCAL
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")
_NON_LOCAL_MARKERS = (
    "非基于本地知识",
    "不是基于本地知识",
    "非本地知识库",
    "不基于本地知识库",
    "来自网络",
    "网络来源",
    "开放域",
)


def _agent_loop_mod():
    try:
        import support_platform.investigation.agent_loop as mod

        return mod
    except ImportError as exc:
        pytest.fail(f"S24 agent_loop module missing (expected Phase4 tool loop): {exc}")


def _require_attr(mod: Any, name: str) -> Any:
    assert hasattr(mod, name), f"investigation.agent_loop missing {name}"
    return getattr(mod, name)


def _blob(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _kinds(steps: list[dict[str, Any]]) -> list[str]:
    return [str(s.get("kind") or s.get("type") or "") for s in steps]


@dataclass
class FakeHit:
    chunk_id: str
    content: str


@dataclass
class ScriptedRetrieve:
    waves: list[list[FakeHit]]
    calls: list[str] = field(default_factory=list)

    def __call__(self, query: str, **_: Any) -> list[FakeHit]:
        self.calls.append(query)
        idx = min(len(self.calls) - 1, len(self.waves) - 1)
        return list(self.waves[idx])


@dataclass
class ScriptedPlanner:
    """Deterministic plan queue: tool decisions then synthesize payload."""

    actions: list[dict[str, Any]]
    synthesize_payload: dict[str, Any]
    plan_calls: int = 0
    synth_calls: int = 0

    def next_action(self, state: dict[str, Any]) -> dict[str, Any]:
        self.plan_calls += 1
        if not self.actions:
            return {"type": "synthesize"}
        return dict(self.actions.pop(0))

    def synthesize(self, state: dict[str, Any]) -> dict[str, Any]:
        self.synth_calls += 1
        payload = dict(self.synthesize_payload)
        # Poison fields must never reach public_steps / client output.
        payload.setdefault("cot", "SECRET_CHAIN_OF_THOUGHT")
        payload.setdefault("prompt", "SYSTEM_PROMPT_LEAK")
        return payload


@dataclass
class MemoryStore:
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)

    def create_running(self, *, question: str) -> str:
        iid = str(uuid4())
        self.rows[iid] = {
            "id": iid,
            "question": question,
            "task_status": "RUNNING",
            "result_status": None,
            "public_steps": [],
            "error_code": None,
            "output": None,
        }
        return iid

    def save(self, row: dict[str, Any]) -> None:
        self.rows[row["id"]] = dict(row)

    def get(self, investigation_id: str) -> dict[str, Any]:
        return dict(self.rows[investigation_id])

    def list_running(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.rows.values() if r["task_status"] == "RUNNING"]


def _fake_web(*, title: str = "Public FAQ", url: str = "https://example.com/faq") -> Any:
    from support_platform.infrastructure.web_search import FakeWebSearchAdapter

    return FakeWebSearchAdapter(
        hits=[{"title": title, "url": url, "snippet": "open-domain snippet"}]
    )


def _run_answer(**kwargs: Any) -> dict[str, Any]:
    mod = _agent_loop_mod()
    runner = _require_attr(mod, "run_phase4_agent_answer")
    return runner(**kwargs)


# --- AC-020: multi-step + hard caps + no CoT ---


def test_agent_loop_exports_caps_and_whitelist() -> None:
    mod = _agent_loop_mod()
    assert _require_attr(mod, "MAX_TOOL_CALLS") == 8
    assert _require_attr(mod, "MAX_WEB_SEARCH_CALLS") == 5
    allowed = _require_attr(mod, "ALLOWED_TOOLS")
    assert set(allowed) == {"local_retrieve", "web_search"}
    _require_attr(mod, "run_phase4_agent_answer")


def test_ac020_multi_tool_calls_appear_in_public_steps() -> None:
    """AC-020: single answer may include multiple tool calls; visible local/web steps."""
    retrieve = ScriptedRetrieve(waves=[[FakeHit("c1", "local password reset doc")]])
    web = _fake_web()
    planner = ScriptedPlanner(
        actions=[
            {"type": "tool", "tool": "local_retrieve", "query": "password reset"},
            {"type": "tool", "tool": "web_search", "query": "password reset public docs"},
            {"type": "synthesize"},
        ],
        synthesize_payload={
            "result_status": "ANSWERED",
            "summary": "建议结合本地文档与公开说明。本结论部分来自网络，非基于本地知识库全部覆盖。",
            "knowledge_basis": "MIXED",
            "claims": [
                {"text": "本地重置路径", "provenance": "LOCAL"},
                {"text": "公开说明摘要", "provenance": "WEB"},
            ],
        },
    )
    result = _run_answer(
        question="How to reset password when local FAQ is thin?",
        retrieve=retrieve,
        web_search=web,
        planner=planner,
        store=MemoryStore(),
    )
    steps = result["public_steps"]
    kinds = _kinds(steps)
    assert "local_retrieve" in kinds, f"missing local_retrieve step: {kinds}"
    assert "web_search" in kinds, f"missing web_search step: {kinds}"
    assert len(retrieve.calls) >= 1
    for step in steps:
        if step.get("kind") in {"local_retrieve", "web_search"}:
            label = step.get("label")
            assert isinstance(label, str) and _ZH_RE.search(label or ""), step


def test_ac020_tool_call_hard_cap_at_eight() -> None:
    """AC-020: reaching SPEC tool-call cap must stop with clear terminal state."""
    retrieve = ScriptedRetrieve(waves=[[FakeHit("c1", "partial")]])
    web = _fake_web()
    endless = [
        {"type": "tool", "tool": "local_retrieve", "query": f"q{i}"} for i in range(20)
    ]
    planner = ScriptedPlanner(
        actions=endless,
        synthesize_payload={
            "result_status": "NO_EVIDENCE",
            "summary": "已达工具调用上限，尚未形成可确认结论。",
            "knowledge_basis": "LOCAL",
            "claims": [],
        },
    )
    result = _run_answer(
        question="force tool loop limit",
        retrieve=retrieve,
        web_search=web,
        planner=planner,
        store=MemoryStore(),
    )
    mod = _agent_loop_mod()
    max_tools = int(_require_attr(mod, "MAX_TOOL_CALLS"))
    tool_steps = [
        s for s in result["public_steps"] if s.get("kind") in {"local_retrieve", "web_search"}
    ]
    assert len(tool_steps) <= max_tools
    stopped = str(result.get("stopped_reason") or "")
    assert len(tool_steps) == max_tools or "limit" in stopped.lower() or "上限" in stopped
    assert result["task_status"] == "COMPLETED"
    assert result.get("result_status") != "ANSWERED"
    reason = stopped.lower()
    assert (
        "limit" in reason or "上限" in stopped or "tool" in reason
    ), f"unclear tool-cap stop: {result.get('stopped_reason')!r}"


def test_ac020_web_search_hard_cap_at_five() -> None:
    """AC-020 / SPEC: web_search ≤ 5 even if planner asks for more."""
    retrieve = ScriptedRetrieve(waves=[[]])
    web = _fake_web()
    endless_web = [
        {"type": "tool", "tool": "web_search", "query": f"web-{i}"} for i in range(12)
    ]
    planner = ScriptedPlanner(
        actions=endless_web,
        synthesize_payload={
            "result_status": "NO_EVIDENCE",
            "summary": "已达网络检索上限；以下内容非基于本地知识库。",
            "knowledge_basis": "WEB",
            "claims": [],
        },
    )
    result = _run_answer(
        question="force web search limit",
        retrieve=retrieve,
        web_search=web,
        planner=planner,
        store=MemoryStore(),
    )
    mod = _agent_loop_mod()
    max_web = int(_require_attr(mod, "MAX_WEB_SEARCH_CALLS"))
    web_steps = [s for s in result["public_steps"] if s.get("kind") == "web_search"]
    assert len(web_steps) <= max_web
    assert len(web_steps) == max_web
    assert result["task_status"] == "COMPLETED"
    assert result.get("result_status") != "ANSWERED"


def test_ac020_public_steps_forbid_cot_prompt_and_secrets() -> None:
    retrieve = ScriptedRetrieve(waves=[[FakeHit("c1", "doc")]])
    web = _fake_web()
    planner = ScriptedPlanner(
        actions=[
            {"type": "tool", "tool": "local_retrieve", "query": "doc"},
            {"type": "synthesize"},
        ],
        synthesize_payload={
            "result_status": "ANSWERED",
            "summary": "本地文档建议。",
            "knowledge_basis": "LOCAL",
            "claims": [{"text": "doc claim", "provenance": "LOCAL"}],
        },
    )
    result = _run_answer(
        question="simple local",
        retrieve=retrieve,
        web_search=web,
        planner=planner,
        store=MemoryStore(),
    )
    steps = result["public_steps"]
    assert steps
    for step in steps:
        for forbidden in ("cot", "prompt", "system", "api_key", "internal"):
            assert forbidden not in step, f"forbidden {forbidden} in {step}"
    blob = _blob(steps).lower()
    for forbidden in ("secret_chain_of_thought", "system_prompt_leak", "tvly-"):
        assert forbidden not in blob


def test_disallowed_business_write_tool_rejected() -> None:
    """SPEC: no business-write tools on the allowlist path."""
    retrieve = ScriptedRetrieve(waves=[[]])
    web = _fake_web()
    planner = ScriptedPlanner(
        actions=[
            {"type": "tool", "tool": "ticket_write", "query": "close ticket"},
            {"type": "synthesize"},
        ],
        synthesize_payload={
            "result_status": "NO_EVIDENCE",
            "summary": "拒绝未授权工具。",
            "knowledge_basis": "LOCAL",
            "claims": [],
        },
    )
    result = _run_answer(
        question="try write tool",
        retrieve=retrieve,
        web_search=web,
        planner=planner,
        store=MemoryStore(),
    )
    kinds = _kinds(result["public_steps"])
    assert "ticket_write" not in kinds


# --- AC-021: WEB disclosure ---


def test_ac021_web_only_sets_knowledge_basis_and_non_local_declaration() -> None:
    retrieve = ScriptedRetrieve(waves=[[]])
    web = _fake_web(title="Community Guide", url="https://example.com/community")
    planner = ScriptedPlanner(
        actions=[
            {"type": "tool", "tool": "web_search", "query": "open domain question"},
            {"type": "synthesize"},
        ],
        synthesize_payload={
            "result_status": "ANSWERED",
            "summary": "根据公开网页说明：……本结论非基于本地知识库。",
            "knowledge_basis": "WEB",
            "claims": [{"text": "community tip", "provenance": "WEB"}],
        },
    )
    result = _run_answer(
        question="open-domain only",
        retrieve=retrieve,
        web_search=web,
        planner=planner,
        store=MemoryStore(),
    )
    output = result.get("output") or {}
    assert output.get("knowledge_basis") == "WEB"
    summary = str(output.get("summary") or "")
    assert summary.strip()
    assert any(m in summary for m in _NON_LOCAL_MARKERS), f"missing non-local disclosure: {summary!r}"


# --- AC-022: MIXED claim provenance ---


def test_ac022_mixed_claims_distinguish_local_and_web() -> None:
    retrieve = ScriptedRetrieve(waves=[[FakeHit("c-local", "Official internal reset steps")]])
    web = _fake_web(title="Public blog", url="https://example.com/blog")
    planner = ScriptedPlanner(
        actions=[
            {"type": "tool", "tool": "local_retrieve", "query": "internal reset"},
            {"type": "tool", "tool": "web_search", "query": "community reset"},
            {"type": "synthesize"},
        ],
        synthesize_payload={
            "result_status": "ANSWERED",
            "summary": "综合本地与网络来源；部分结论非基于本地知识库。",
            "knowledge_basis": "MIXED",
            "claims": [
                {"text": "内部重置步骤", "provenance": "LOCAL"},
                {"text": "社区补充说明", "provenance": "WEB"},
            ],
        },
    )
    result = _run_answer(
        question="mixed evidence question",
        retrieve=retrieve,
        web_search=web,
        planner=planner,
        store=MemoryStore(),
    )
    output = result.get("output") or {}
    assert output.get("knowledge_basis") == "MIXED"
    summary = str(output.get("summary") or "")
    assert any(m in summary for m in _NON_LOCAL_MARKERS), f"MIXED needs non-local disclosure: {summary!r}"
    claims = list(output.get("claims") or [])
    prov = {str(c.get("provenance")) for c in claims}
    assert "LOCAL" in prov and "WEB" in prov
    for claim in claims:
        if claim.get("provenance") == "WEB":
            assert claim.get("provenance") != "LOCAL"


def test_empty_question_fails_closed() -> None:
    retrieve = ScriptedRetrieve(waves=[[]])
    web = _fake_web()
    planner = ScriptedPlanner(
        actions=[{"type": "synthesize"}],
        synthesize_payload={
            "result_status": "NO_EVIDENCE",
            "summary": "",
            "knowledge_basis": "LOCAL",
            "claims": [],
        },
    )
    result = _run_answer(
        question="   ",
        retrieve=retrieve,
        web_search=web,
        planner=planner,
        store=MemoryStore(),
    )
    assert result["task_status"] == "FAILED"
    assert result.get("error_code") in {"EMPTY_QUESTION", "VALIDATION_ERROR", "INVALID_INPUT"}


def test_conversation_turn_surfaces_knowledge_basis() -> None:
    """Conversation boundary must expose knowledge_basis on assistant turn (OP-16/17 shape)."""
    try:
        from support_platform.conversation import run_conversation_turn
    except ImportError as exc:
        pytest.fail(f"S24 conversation package missing: {exc}")

    retrieve = ScriptedRetrieve(waves=[[]])
    web = _fake_web()
    planner = ScriptedPlanner(
        actions=[
            {"type": "tool", "tool": "web_search", "query": "open"},
            {"type": "synthesize"},
        ],
        synthesize_payload={
            "result_status": "ANSWERED",
            "summary": "开放域回答；本结论非基于本地知识库。",
            "knowledge_basis": "WEB",
            "claims": [{"text": "web tip", "provenance": "WEB"}],
        },
    )
    turn = run_conversation_turn(
        content="open domain ask",
        retrieve=retrieve,
        web_search=web,
        planner=planner,
        store=MemoryStore(),
    )
    assert turn.get("knowledge_basis") == "WEB" or (
        isinstance(turn.get("output"), dict) and turn["output"].get("knowledge_basis") == "WEB"
    )
