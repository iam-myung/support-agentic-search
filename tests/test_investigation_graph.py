"""S4 investigation graph / one re-query RED contracts (REQ-002 / AC-002)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class FakeHit:
    chunk_id: str
    content: str
    channels: tuple[str, ...] = ("semantic",)
    channel_ranks: dict[str, int] = field(default_factory=lambda: {"semantic": 1})


@dataclass
class ScriptedRetrieve:
    """Returns successive hit lists; records every call."""

    waves: list[list[FakeHit]]
    calls: list[str] = field(default_factory=list)

    def __call__(self, query: str, **_: Any) -> list[FakeHit]:
        self.calls.append(query)
        idx = min(len(self.calls) - 1, len(self.waves) - 1)
        return list(self.waves[idx])


@dataclass
class ScriptedChat:
    """Deterministic assess/rewrite responses for graph branching."""

    assess_actions: list[str]
    rewrite_to: str = "rewritten query about password reset"
    assess_calls: int = 0
    rewrite_calls: int = 0
    last_system_leak_probe: str | None = None

    def assess(self, *, question: str, hits: list[Any], public_only: bool = True) -> dict[str, Any]:
        self.assess_calls += 1
        action = self.assess_actions[min(self.assess_calls - 1, len(self.assess_actions) - 1)]
        gaps = ["need second source on reset steps"] if action == "resolvable_gap" else []
        return {
            "next_action": action,
            "gaps": gaps,
            "candidate_claims": [],
            "supporting_ids": [h.chunk_id for h in hits],
            "conflicting_ids": [],
        }

    def rewrite(self, *, question: str, constraints: dict[str, Any], gaps: list[str]) -> str:
        self.rewrite_calls += 1
        # GREEN must not invent customer facts beyond question/constraints/gaps.
        return self.rewrite_to

    def synthesize(
        self,
        *,
        question: str,
        hits: list[Any],
        assessment: dict[str, Any],
        result_status: str,
    ) -> dict[str, Any]:
        action = (assessment or {}).get("next_action")
        status = "ANSWERED" if action == "sufficient" else result_status
        if action == "conflict":
            status = "CONFLICT"
        return {
            "result_status": status,
            "summary": "根据授权资料给出的处理建议。",
            "claims": [],
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


def test_resolvable_gap_triggers_requery_within_phase3_cap() -> None:
    from support_platform.investigation import run_investigation

    retrieve = ScriptedRetrieve(
        waves=[
            [FakeHit("c1", "Partial note about accounts")],
            [FakeHit("c1", "Partial note about accounts"), FakeHit("c2", "Reset password in Settings")],
        ]
    )
    chat = ScriptedChat(
        assess_actions=["resolvable_gap", "sufficient"],
        rewrite_to="reset password settings steps",
    )
    store = MemoryStore()
    result = run_investigation(
        question="How do I change my password?",
        retrieve=retrieve,
        chat=chat,
        store=store,
    )
    assert len(retrieve.calls) == 2
    assert chat.rewrite_calls == 1
    assert result["task_status"] == "COMPLETED"
    steps = result["public_steps"]
    kinds = [s.get("kind") or s.get("type") for s in steps]
    blob = " ".join(str(k) for k in kinds).lower()
    assert "retrieve" in blob
    assert any(s.get("label") for s in steps)


def test_continuous_gap_stops_at_three_retrieve_rounds() -> None:
    """Phase3: at most 3 retrieve rounds even if assess keeps asking for more."""
    from support_platform.investigation import run_investigation

    retrieve = ScriptedRetrieve(
        waves=[
            [FakeHit("c1", "Only billing")],
            [FakeHit("c1", "Only billing"), FakeHit("c2", "more")],
            [FakeHit("c1", "Only billing"), FakeHit("c2", "more"), FakeHit("c3", "more2")],
            [FakeHit("c4", "must not")],
        ]
    )
    chat = ScriptedChat(
        assess_actions=["resolvable_gap", "resolvable_gap", "resolvable_gap", "resolvable_gap"],
        rewrite_to="billing again",
    )
    result = run_investigation(
        question="Why was I charged twice?",
        retrieve=retrieve,
        chat=chat,
        store=MemoryStore(),
    )
    assert len(retrieve.calls) == 3
    assert chat.rewrite_calls == 2
    assert result["task_status"] in {"COMPLETED", "FAILED"}
    assert result.get("result_status") != "ANSWERED"


def test_no_new_evidence_after_requery_stops() -> None:
    from support_platform.investigation import run_investigation

    same = [FakeHit("c1", "Same doc only")]
    retrieve = ScriptedRetrieve(waves=[same, same])
    chat = ScriptedChat(assess_actions=["resolvable_gap", "resolvable_gap"])
    result = run_investigation(
        question="Need more sources",
        retrieve=retrieve,
        chat=chat,
        store=MemoryStore(),
    )
    assert len(retrieve.calls) == 2
    assert chat.rewrite_calls == 1
    assert result["task_status"] == "COMPLETED"
    assert result.get("stopped_reason") in {
        "no_new_evidence",
        "NO_NEW_EVIDENCE",
        "no_evidence",
        "NO_EVIDENCE",
    } or result.get("result_status") in {"NO_EVIDENCE", "NEEDS_HUMAN", None}


def test_public_steps_omit_internal_reasoning_and_secrets() -> None:
    from support_platform.investigation import run_investigation

    retrieve = ScriptedRetrieve(
        waves=[
            [FakeHit("c1", "Hint only")],
            [FakeHit("c2", "Reset password from Settings > Security")],
        ]
    )
    chat = ScriptedChat(assess_actions=["resolvable_gap", "sufficient"])
    result = run_investigation(
        question="password reset",
        retrieve=retrieve,
        chat=chat,
        store=MemoryStore(),
    )
    steps = result["public_steps"]
    assert isinstance(steps, list) and len(steps) >= 2
    blob = json_dumps_lower(steps)
    for forbidden in (
        "chain of thought",
        "internal reasoning",
        "system prompt",
        "api_key",
        "sk-",
        "ignore all previous",
        "hidden_cot",
    ):
        assert forbidden not in blob
    assert "retrieve" in blob or "检索" in blob
    assert "gap" in blob or "缺口" in blob or "补查" in blob or "plan" in blob or "比较" in blob


def test_stale_running_marked_failed_process_interrupted() -> None:
    from support_platform.investigation import fail_stale_running

    store = MemoryStore()
    iid = store.create_running(question="orphan job")
    assert store.get(iid)["task_status"] == "RUNNING"
    changed = fail_stale_running(store)
    assert iid in changed
    row = store.get(iid)
    assert row["task_status"] == "FAILED"
    assert row["error_code"] == "PROCESS_INTERRUPTED"
    assert row["result_status"] is None
    # Public steps already completed must be preserved (empty ok for fresh orphan)
    assert isinstance(row["public_steps"], list)


def json_dumps_lower(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False).lower()
