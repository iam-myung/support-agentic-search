"""S13 HITL interrupt / resume / cancel RED (REQ-009 / AC-011).

Focus (SPEC §4 OP-10/11; §5 interrupt+idempotency; §8 S13):
- illegal resume (non-INTERRUPTED / wrong interrupt_id) → 409
- duplicate human action with same idempotency key does not double-apply
- REJECT must not be recorded as execution success
- cancel → CANCELLED, evidence/public steps retained
- human_needed path interrupts with stable interrupt_id; no irreversible
  side effects before the interrupt gate
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text


def _load_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == "DATABASE_URL":
                return value.strip().strip('"').strip("'")
    raise AssertionError("DATABASE_URL must be set for S13 HITL contracts")


def _fresh_app_client(monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", _load_database_url())
    import support_platform.config as config_mod
    import support_platform.main as main_mod

    importlib.reload(config_mod)
    importlib.reload(main_mod)
    return TestClient(main_mod.app)


def _login(client: TestClient, *, username: str) -> tuple[dict[str, str], str]:
    from support_platform.auth.testing import ensure_user, login_session

    ensure_user(username=username, password="secret-ok", role="SUPPORT_AGENT")
    return login_session(client, username=username, password="secret-ok")


def _create_queued(
    client: TestClient,
    *,
    cookies: dict[str, str],
    csrf: str,
    question: str,
) -> str:
    resp = client.post(
        "/api/v2/investigations",
        json={"question": question, "context": {}},
        cookies=cookies,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": f"s13-create-{uuid4()}"},
    )
    assert resp.status_code == 202, f"OP-07 seed failed: {resp.status_code} {resp.text}"
    return str(resp.json()["id"])


def _force_interrupted(
    *,
    investigation_id: str,
    interrupt_id: str,
    public_steps: list[dict[str, Any]] | None = None,
) -> None:
    """Test fixture: place task into INTERRUPTED with a known interrupt_id.

    GREEN may replace this with task_runtime.mark_interrupted; SQL seed is only
    for establishing preconditions so HTTP assertions can target OP-10/11.
    """
    steps = list(public_steps or [])
    steps.append(
        {
            "kind": "review_required",
            "interrupt_id": interrupt_id,
            "reason": "human_needed",
        }
    )
    engine = create_engine(_load_database_url(), pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE investigations
                SET task_status = 'INTERRUPTED',
                    result_status = NULL,
                    public_steps = CAST(:steps AS jsonb),
                    output = NULL
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {
                "id": investigation_id,
                "steps": json.dumps(steps, ensure_ascii=False),
            },
        )
    # Durable checkpoint carrying interrupt metadata (SPEC §5 thread_id = task id).
    from support_platform.task_runtime.checkpointer import PostgresCheckpointerAdapter

    adapter = PostgresCheckpointerAdapter(database_url=_load_database_url())
    adapter.put_checkpoint(
        thread_id=str(investigation_id),
        checkpoint_id=f"ckpt-hitl-{interrupt_id}",
        payload={
            "interrupt_id": interrupt_id,
            "public_steps": steps,
            "awaiting": "human_decision",
        },
    )


# --- OP-10 illegal resume ---


def test_op10_resume_on_non_interrupted_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = _login(client, username="s13-agent-a")
    inv_id = _create_queued(
        client, cookies=cookies, csrf=csrf, question="S13-RED resume while QUEUED"
    )

    resp = client.post(
        f"/api/v2/investigations/{inv_id}/resume",
        json={
            "interrupt_id": str(uuid4()),
            "action": "SUPPLY",
            "input": {"note": "extra context"},
        },
        cookies=cookies,
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": f"s13-resume-{uuid4()}",
        },
    )
    assert resp.status_code == 409, (
        f"OP-10 on non-INTERRUPTED must 409, got {resp.status_code} {resp.text}"
    )


def test_op10_wrong_interrupt_id_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = _login(client, username="s13-agent-b")
    inv_id = _create_queued(
        client, cookies=cookies, csrf=csrf, question="S13-RED wrong interrupt_id"
    )
    real_interrupt = f"intr-{uuid4()}"
    _force_interrupted(investigation_id=inv_id, interrupt_id=real_interrupt)

    resp = client.post(
        f"/api/v2/investigations/{inv_id}/resume",
        json={
            "interrupt_id": f"wrong-{uuid4()}",
            "action": "APPROVE",
            "input": {},
        },
        cookies=cookies,
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": f"s13-resume-bad-{uuid4()}",
        },
    )
    assert resp.status_code == 409, (
        f"OP-10 wrong interrupt_id must 409, got {resp.status_code} {resp.text}"
    )


def test_op10_valid_supply_returns_202_and_leaves_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = _login(client, username="s13-agent-c")
    inv_id = _create_queued(
        client, cookies=cookies, csrf=csrf, question="S13-RED valid SUPPLY"
    )
    interrupt_id = f"intr-{uuid4()}"
    _force_interrupted(investigation_id=inv_id, interrupt_id=interrupt_id)

    resp = client.post(
        f"/api/v2/investigations/{inv_id}/resume",
        json={
            "interrupt_id": interrupt_id,
            "action": "SUPPLY",
            "input": {"product_version": "2.1"},
        },
        cookies=cookies,
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": f"s13-supply-{uuid4()}",
        },
    )
    assert resp.status_code == 202, (
        f"OP-10 valid SUPPLY must 202, got {resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert body.get("id") == inv_id
    # After accepted human input, task must leave INTERRUPTED (QUEUED/RUNNING for resume).
    assert body.get("task_status") in {"QUEUED", "RUNNING"}, (
        f"after SUPPLY expected QUEUED|RUNNING, got {body.get('task_status')}"
    )


# --- Idempotency / REJECT / cancel ---


def test_op10_duplicate_idempotency_key_same_body_is_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = _login(client, username="s13-agent-d")
    inv_id = _create_queued(
        client, cookies=cookies, csrf=csrf, question="S13-RED duplicate human action"
    )
    interrupt_id = f"intr-{uuid4()}"
    _force_interrupted(investigation_id=inv_id, interrupt_id=interrupt_id)
    idem = f"s13-human-idem-{uuid4()}"
    payload = {
        "interrupt_id": interrupt_id,
        "action": "EDIT",
        "input": {"suggested_reply": "please try again after cache clear"},
    }

    first = client.post(
        f"/api/v2/investigations/{inv_id}/resume",
        json=payload,
        cookies=cookies,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": idem},
    )
    second = client.post(
        f"/api/v2/investigations/{inv_id}/resume",
        json=payload,
        cookies=cookies,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": idem},
    )
    assert first.status_code == 202 and second.status_code == 202, (
        f"duplicate same-body OP-10 must both 202; got {first.status_code}/{second.status_code}"
    )
    assert first.json().get("id") == second.json().get("id") == inv_id

    # Conflicting body under same key must 409 (not silently mutate).
    conflict = client.post(
        f"/api/v2/investigations/{inv_id}/resume",
        json={
            "interrupt_id": interrupt_id,
            "action": "APPROVE",
            "input": {},
        },
        cookies=cookies,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": idem},
    )
    assert conflict.status_code == 409, (
        f"OP-10 same key different body must 409, got {conflict.status_code}"
    )


def test_op10_reject_does_not_record_execution_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = _login(client, username="s13-agent-e")
    inv_id = _create_queued(
        client, cookies=cookies, csrf=csrf, question="S13-RED REJECT not success"
    )
    interrupt_id = f"intr-{uuid4()}"
    steps_before = [
        {"kind": "retrieve_1", "hit_count": 2},
        {"kind": "review_required", "interrupt_id": interrupt_id},
    ]
    _force_interrupted(
        investigation_id=inv_id,
        interrupt_id=interrupt_id,
        public_steps=[{"kind": "retrieve_1", "hit_count": 2}],
    )

    resp = client.post(
        f"/api/v2/investigations/{inv_id}/resume",
        json={
            "interrupt_id": interrupt_id,
            "action": "REJECT",
            "input": {"reason": "customer declined"},
        },
        cookies=cookies,
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": f"s13-reject-{uuid4()}",
        },
    )
    assert resp.status_code == 202, (
        f"OP-10 REJECT must 202, got {resp.status_code} {resp.text}"
    )

    got = client.get(f"/api/v2/investigations/{inv_id}", cookies=cookies)
    assert got.status_code == 200, got.text
    body = got.json()
    # REJECT must terminate without execution success (AC-011).
    assert body.get("task_status") != "COMPLETED" or body.get("result_status") not in {
        "ANSWERED",
    }, "REJECT must not be recorded as successful COMPLETED/ANSWERED"
    assert body.get("task_status") in {"FAILED", "CANCELLED", "COMPLETED"}, (
        f"REJECT must reach a terminal non-success path, got {body.get('task_status')}"
    )
    if body.get("task_status") == "COMPLETED":
        assert body.get("result_status") in {None, "NEEDS_HUMAN"}, (
            f"REJECT COMPLETED only allowed with non-success result, got {body.get('result_status')}"
        )
    # Prior public steps must remain.
    steps = body.get("public_steps") or []
    assert any(s.get("kind") == "retrieve_1" for s in steps), (
        f"REJECT must retain prior public steps, got {steps}"
    )
    # No successful final_output node.
    from support_platform.task_runtime import count_final_outputs

    n = count_final_outputs(database_url=_load_database_url(), investigation_id=inv_id)
    assert n == 0, f"REJECT must not create final_output success row, count={n}"
    _ = steps_before  # documented expected prior kinds


def test_op11_cancel_sets_cancelled_and_keeps_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = _login(client, username="s13-agent-f")
    inv_id = _create_queued(
        client, cookies=cookies, csrf=csrf, question="S13-RED cancel preserves steps"
    )
    interrupt_id = f"intr-{uuid4()}"
    _force_interrupted(
        investigation_id=inv_id,
        interrupt_id=interrupt_id,
        public_steps=[{"kind": "retrieve_1", "hit_count": 1}],
    )

    resp = client.post(
        f"/api/v2/investigations/{inv_id}/cancel",
        json={},
        cookies=cookies,
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": f"s13-cancel-{uuid4()}",
        },
    )
    assert resp.status_code == 202, (
        f"OP-11 must 202, got {resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert body.get("task_status") == "CANCELLED", (
        f"OP-11 must yield CANCELLED, got {body.get('task_status')}"
    )

    got = client.get(f"/api/v2/investigations/{inv_id}", cookies=cookies)
    assert got.status_code == 200
    fact = got.json()
    assert fact.get("task_status") == "CANCELLED"
    steps = fact.get("public_steps") or []
    assert any(s.get("kind") == "retrieve_1" for s in steps), (
        "cancel must not erase public steps / evidence trail"
    )


# --- Graph interrupt gate (no irreversible side effect before interrupt) ---


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
    assess_actions: list[str]
    assess_calls: int = 0
    irreversible_writes: int = 0

    def assess(self, *, question: str, hits: list[Any], public_only: bool = True) -> dict[str, Any]:
        self.assess_calls += 1
        action = self.assess_actions[min(self.assess_calls - 1, len(self.assess_actions) - 1)]
        return {
            "next_action": action,
            "gaps": ["need customer tenant id"],
            "candidate_claims": [],
            "supporting_ids": [h.chunk_id for h in hits],
            "conflicting_ids": [],
        }

    def rewrite(self, *, question: str, constraints: dict[str, Any], gaps: list[str]) -> str:
        return "rewritten with tenant gap"

    def write_ticket(self, **_: Any) -> None:
        """Must never be invoked by HITL graph (high-risk write tool banned)."""
        self.irreversible_writes += 1


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
            "interrupt_id": None,
        }
        return iid

    def save(self, row: dict[str, Any]) -> None:
        self.rows[row["id"]] = dict(row)

    def get(self, investigation_id: str) -> dict[str, Any]:
        return dict(self.rows[investigation_id])

    def list_running(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.rows.values() if r["task_status"] == "RUNNING"]


def test_human_needed_path_interrupts_with_stable_interrupt_id() -> None:
    """SPEC §5/§8 S13: human_needed → INTERRUPTED + interrupt_id, not silent COMPLETED success."""
    from support_platform.investigation import run_investigation

    retrieve = ScriptedRetrieve(waves=[[FakeHit("c-hitl", "Need tenant id before advise")]])
    chat = ScriptedChat(assess_actions=["human_needed"])
    store = MemoryStore()

    result = run_investigation(
        question="How do I reset SSO for tenant X?",
        retrieve=retrieve,
        chat=chat,
        store=store,
    )
    assert result["task_status"] == "INTERRUPTED", (
        f"human_needed must INTERRUPT, got task_status={result.get('task_status')!r} "
        f"result_status={result.get('result_status')!r}"
    )
    interrupt_id = result.get("interrupt_id")
    if not interrupt_id:
        # Also accept interrupt_id embedded in public_steps (OP-08 shape).
        for step in result.get("public_steps") or []:
            if step.get("interrupt_id"):
                interrupt_id = step["interrupt_id"]
                break
    assert interrupt_id, "interrupted investigation must expose a stable interrupt_id"
    assert chat.irreversible_writes == 0, (
        "no high-risk / irreversible write tool may run before HITL interrupt gate"
    )


def test_hitl_module_exposes_mark_interrupted_and_apply_decision() -> None:
    """Domain helpers required so worker/API share one interrupt contract."""
    try:
        from support_platform.investigation import hitl as hitl_mod
    except ImportError:
        hitl_mod = None
    if hitl_mod is None:
        try:
            from support_platform.task_runtime import service as hitl_mod
        except ImportError as exc:
            raise AssertionError(
                "HITL helpers missing: expected investigation.hitl or task_runtime.service"
            ) from exc

    mark = getattr(hitl_mod, "mark_interrupted", None)
    apply = getattr(hitl_mod, "apply_human_decision", None) or getattr(
        hitl_mod, "resume_from_interrupt", None
    )
    cancel = getattr(hitl_mod, "request_cancel", None) or getattr(hitl_mod, "cancel_investigation", None)
    assert callable(mark), "mark_interrupted helper required for stable interrupt_id + checkpoint"
    assert callable(apply), "apply_human_decision / resume_from_interrupt required for OP-10"
    assert callable(cancel), "request_cancel / cancel_investigation required for OP-11"


def test_resume_idempotency_keys_are_persisted_separately_from_create() -> None:
    """Human-action idempotency must not collide with OP-07 create keys."""
    from support_platform.infrastructure.db.models import metadata

    # Prefer a dedicated table or uniquely scoped keys under node_executions.
    tables = set(metadata.tables.keys())
    has_hitl_keys = "investigation_resume_keys" in tables or "human_decision_keys" in tables
    has_node = "node_executions" in tables
    assert has_hitl_keys or has_node, (
        "must persist resume/cancel idempotency (dedicated table or node_executions)"
    )
    # Contract probe: helper that records a human decision key must exist.
    try:
        from support_platform.investigation import hitl as hitl_mod
    except ImportError:
        from support_platform.task_runtime import service as hitl_mod

    remember = getattr(hitl_mod, "remember_resume_idempotency", None) or getattr(
        hitl_mod, "lookup_resume_idempotency", None
    )
    assert callable(remember) or callable(
        getattr(hitl_mod, "apply_human_decision", None)
    ), "resume idempotency must be implemented beside OP-10"


def test_interrupt_id_is_stable_across_helper_calls() -> None:
    """Same interrupted task must keep the same interrupt_id until resolved."""
    try:
        from support_platform.investigation import hitl as hitl_mod
    except ImportError:
        from support_platform.task_runtime import service as hitl_mod

    mark = getattr(hitl_mod, "mark_interrupted", None)
    assert callable(mark), "mark_interrupted required"

    database_url = _load_database_url()
    from support_platform.task_runtime import create_queued_investigation, get_task

    iid = uuid4()
    owner = uuid4()
    create_queued_investigation(
        database_url=database_url,
        investigation_id=iid,
        question="S13-RED stable interrupt_id",
        context={},
        owner_user_id=owner,
    )
    first = mark(
        database_url=database_url,
        investigation_id=iid,
        reason="human_needed",
        public_steps=[{"kind": "assess", "next_action": "human_needed"}],
    )
    second = mark(
        database_url=database_url,
        investigation_id=iid,
        reason="human_needed",
        public_steps=[{"kind": "assess", "next_action": "human_needed"}],
    )
    assert first.get("interrupt_id") and first["interrupt_id"] == second.get("interrupt_id"), (
        f"interrupt_id must be stable until resume/cancel; got {first!r} vs {second!r}"
    )
    task = get_task(database_url=database_url, investigation_id=iid)
    assert task is not None and task["task_status"] == "INTERRUPTED"
    # Determinism check: interrupt_id should be derivable / stored, not random each call.
    assert isinstance(first["interrupt_id"], str) and len(first["interrupt_id"]) >= 8
    _ = hashlib.sha256(str(iid).encode()).hexdigest()  # documents stability expectation
