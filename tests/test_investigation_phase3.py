"""S20 Phase3 investigation graph RED contracts (REQ-013 / AC-015～017)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

# Reuse lightweight doubles from S4 suite shape (no shared import of production).
@dataclass
class FakeHit:
    chunk_id: str
    content: str
    channels: tuple[str, ...] = ("semantic",)
    channel_ranks: dict[str, int] = field(default_factory=lambda: {"semantic": 1})


@dataclass
class ScriptedRetrieve:
    waves: list[list[FakeHit]]
    calls: list[str] = field(default_factory=list)

    def __call__(self, query: str, **_: Any) -> list[FakeHit]:
        self.calls.append(query)
        idx = min(len(self.calls) - 1, len(self.waves) - 1)
        return list(self.waves[idx])


@dataclass
class ScriptedChat:
    """Deterministic chat for Phase3 branching + optional synthesize."""

    assess_actions: list[str]
    rewrite_to: str = "rewritten subquery about password reset"
    synthesize_summary: str = ""
    synthesize_status: str = "ANSWERED"
    assess_calls: int = 0
    rewrite_calls: int = 0
    synthesize_calls: int = 0

    def assess(self, *, question: str, hits: list[Any], public_only: bool = True) -> dict[str, Any]:
        self.assess_calls += 1
        action = self.assess_actions[min(self.assess_calls - 1, len(self.assess_actions) - 1)]
        gaps = ["need another authorized source"] if action == "resolvable_gap" else []
        conflicting = ["c-a", "c-b"] if action == "conflict" else []
        return {
            "next_action": action,
            "gaps": gaps,
            "candidate_claims": [],
            "supporting_ids": [getattr(h, "chunk_id", h) for h in hits],
            "conflicting_ids": conflicting,
            # Must never appear in public_steps even if model returns these keys.
            "cot": "SECRET_CHAIN_OF_THOUGHT",
            "prompt": "SYSTEM_PROMPT_LEAK",
        }

    def rewrite(self, *, question: str, constraints: dict[str, Any], gaps: list[str]) -> str:
        self.rewrite_calls += 1
        return f"{self.rewrite_to} #{self.rewrite_calls}"

    def synthesize(
        self,
        *,
        question: str,
        hits: list[Any],
        assessment: dict[str, Any],
        result_status: str,
    ) -> dict[str, Any]:
        self.synthesize_calls += 1
        return {
            "result_status": result_status or self.synthesize_status,
            "summary": self.synthesize_summary,
            "claims": [],
            "cot": "MUST_NOT_PERSIST",
            "prompt": "MUST_NOT_PERSIST",
        }


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


_ZH_RE = re.compile(r"[\u4e00-\u9fff]")


def _kinds(steps: list[dict[str, Any]]) -> list[str]:
    return [str(s.get("kind") or s.get("type") or "") for s in steps]


def _blob(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False).lower()


def test_phase3_public_steps_cover_agentic_flow_with_zh_labels() -> None:
    """AC-015: understand/plan/retrieve/compare/synthesize + Chinese label."""
    from support_platform.investigation import run_investigation

    retrieve = ScriptedRetrieve(waves=[[FakeHit("c1", "Reset password in Settings")]])
    chat = ScriptedChat(
        assess_actions=["sufficient"],
        synthesize_summary="在设置中重置密码。",
        synthesize_status="ANSWERED",
    )
    result = run_investigation(
        question="How do I reset my password?",
        retrieve=retrieve,
        chat=chat,
        store=MemoryStore(),
    )
    steps = result["public_steps"]
    assert isinstance(steps, list) and len(steps) >= 3
    kinds = " ".join(_kinds(steps)).lower()
    for token in ("understand", "plan", "retrieve", "compare", "synthesize"):
        assert token in kinds, f"missing public step kind covering {token}: {kinds}"
    for step in steps:
        label = step.get("label")
        assert isinstance(label, str) and label.strip(), f"missing zh label: {step}"
        assert _ZH_RE.search(label), f"label must be Chinese-visible: {label!r}"


def test_phase3_retrieve_rounds_cap_at_three_then_stop_not_cleared() -> None:
    """AC-017: up to 3 retrieve rounds; exceeding must stop without fake ANSWERED."""
    from support_platform.investigation import run_investigation

    retrieve = ScriptedRetrieve(
        waves=[
            [FakeHit("c1", "partial A")],
            [FakeHit("c1", "partial A"), FakeHit("c2", "partial B")],
            [FakeHit("c1", "partial A"), FakeHit("c2", "partial B"), FakeHit("c3", "partial C")],
            [FakeHit("c4", "must not be reached")],
        ]
    )
    chat = ScriptedChat(
        assess_actions=["resolvable_gap", "resolvable_gap", "resolvable_gap", "resolvable_gap"],
        synthesize_summary="",
    )
    result = run_investigation(
        question="Need multi-source investigation",
        retrieve=retrieve,
        chat=chat,
        store=MemoryStore(),
    )
    assert len(retrieve.calls) == 3, f"expected exactly 3 retrieve rounds, got {len(retrieve.calls)}"
    assert len(retrieve.calls) <= 3
    assert result["task_status"] == "COMPLETED"
    # Must not pretend the case is fully answered after hitting the round cap.
    assert result.get("result_status") != "ANSWERED"
    reason = str(result.get("stopped_reason") or "").lower()
    status = str(result.get("result_status") or "").upper()
    assert (
        "limit" in reason
        or "round" in reason
        or "上限" in reason
        or status in {"NO_EVIDENCE", "NEEDS_HUMAN", "CONFLICT"}
    ), f"round-cap stop unclear: status={status} reason={reason}"


def test_phase3_answered_requires_nonempty_summary() -> None:
    """AC-016: ANSWERED → output.summary human-readable non-empty."""
    from support_platform.investigation import run_investigation

    retrieve = ScriptedRetrieve(waves=[[FakeHit("c1", "Reset password in Settings > Security")]])
    chat = ScriptedChat(
        assess_actions=["sufficient"],
        synthesize_summary="请到「设置 > 安全」重置密码。",
        synthesize_status="ANSWERED",
    )
    result = run_investigation(
        question="password reset path",
        retrieve=retrieve,
        chat=chat,
        store=MemoryStore(),
    )
    assert result["task_status"] == "COMPLETED"
    assert result.get("result_status") == "ANSWERED"
    output = result.get("output") or {}
    summary = output.get("summary")
    assert isinstance(summary, str) and summary.strip(), f"missing summary: {output}"


def test_phase3_conflict_summary_outlines_both_sides() -> None:
    """AC-016: CONFLICT → non-empty summary covering both sides."""
    from support_platform.investigation import run_investigation

    retrieve = ScriptedRetrieve(
        waves=[
            [
                FakeHit("c-a", "MFA is ALWAYS required"),
                FakeHit("c-b", "MFA is NEVER required"),
            ]
        ]
    )
    chat = ScriptedChat(
        assess_actions=["conflict"],
        synthesize_summary="冲突：一方称必须 MFA，另一方称从不需要 MFA。",
        synthesize_status="CONFLICT",
    )
    result = run_investigation(
        question="Is MFA required for password reset?",
        retrieve=retrieve,
        chat=chat,
        store=MemoryStore(),
    )
    assert result.get("result_status") == "CONFLICT"
    summary = ((result.get("output") or {}).get("summary") or "").strip()
    assert summary, "CONFLICT requires non-empty summary"
    lowered = summary.lower()
    assert ("冲突" in summary) or ("conflict" in lowered) or ("一方" in summary)
    assert ("mfa" in lowered) or ("双方" in summary) or ("另一方" in summary)


def test_phase3_public_steps_forbid_cot_and_prompt_fields() -> None:
    """AC-015 / SPEC: cot/prompt/api_key/internal must not appear in public_steps."""
    from support_platform.investigation import run_investigation

    retrieve = ScriptedRetrieve(waves=[[FakeHit("c1", "doc")]])
    chat = ScriptedChat(assess_actions=["sufficient"], synthesize_summary="建议正文")
    result = run_investigation(
        question="simple",
        retrieve=retrieve,
        chat=chat,
        store=MemoryStore(),
    )
    steps = result["public_steps"]
    assert steps, "expected public_steps"
    for step in steps:
        for forbidden in ("cot", "prompt", "system", "api_key", "internal"):
            assert forbidden not in step, f"forbidden field {forbidden} in {step}"
    blob = _blob(steps)
    for forbidden in ("secret_chain_of_thought", "system_prompt_leak", "must_not_persist"):
        assert forbidden not in blob
