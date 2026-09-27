"""S21 Phase3 workbench UI RED (REQ-013 / AC-015～016 UI; REQ-008 SSE label).

Focus (SPEC Phase3 UI + §8 S21):
- result/failure timeline must render Chinese public_steps.label
- ANSWERED/CONFLICT must show nonempty output.summary in suggestion area
- STEP SSE client must prefer public_payload.label
- sensitive keys (cot/prompt/api_key/internal) must not appear in visible DOM
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[1]
_STATIC = _REPO / "src" / "support_platform" / "static"
_TEMPLATES = _REPO / "src" / "support_platform" / "templates"
_FORBIDDEN = ("prompt", "system", "cot", "api_key", "internal")


def _client() -> TestClient:
    from support_platform.main import app

    return TestClient(app)


def _ok_service(
    *,
    result_status: str,
    summary: str,
    steps: list[dict[str, Any]],
) -> Any:
    iid = str(uuid4())

    class OkService:
        investigation_id = iid

        def create_investigation(
            self, *, question: str, context: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return {
                "id": iid,
                "task_status": "COMPLETED",
                "result_status": result_status,
                "public_steps": list(steps),
                "claims": [],
                "evidence": [],
                "output": {"summary": summary},
            }

        def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
            if investigation_id != iid:
                return None
            return self.create_investigation(question="x")

        def get_evidence(
            self, investigation_id: str, evidence_id: str
        ) -> dict[str, Any] | None:
            return None

    return OkService()


def test_workbench_result_timeline_shows_chinese_labels() -> None:
    """AC-015: completed result timeline must show Chinese public_steps.label."""
    from support_platform.api import investigations as inv_api

    steps = [
        {"kind": "understand_goal", "label": "理解目标"},
        {"kind": "plan_subqueries", "label": "拆分子查询"},
        {"kind": "retrieve", "label": "检索 1/3"},
        {"kind": "compare_evidence", "label": "比较证据"},
        {"kind": "synthesize", "label": "生成建议"},
    ]
    svc = _ok_service(
        result_status="ANSWERED",
        summary="请到设置中重置密码。",
        steps=steps,
    )
    inv_api.set_investigation_service(svc)
    try:
        client = _client()
        response = client.post(
            "/workbench",
            data={"question": "How do I reset my password?"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        body = response.text
        assert "ANSWERED" in body
        # Phase3: labels visible — kind-only timeline is insufficient.
        for label in ("理解目标", "检索 1/3", "比较证据", "生成建议"):
            assert label in body, f"missing zh timeline label in DOM: {label!r}"
        # Template contract: render step.label (not kind-only).
        tpl = (_TEMPLATES / "workbench.html").read_text(encoding="utf-8")
        assert "step.label" in tpl or "step['label']" in tpl, (
            "workbench.html must render public_steps[].label for the timeline"
        )
    finally:
        inv_api.reset_investigation_service()


def test_workbench_failure_timeline_shows_chinese_labels() -> None:
    """AC-015: failure panel retained steps must also show Chinese label."""
    from support_platform.api import investigations as inv_api

    iid = str(uuid4())
    steps = [
        {"kind": "understand_goal", "label": "理解目标"},
        {"kind": "retrieve", "label": "检索 1/3"},
    ]

    class FailService:
        def create_investigation(
            self, *, question: str, context: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            raise inv_api.InvestigationDependencyError(
                investigation_id=iid,
                error_code="CHAT_UNAVAILABLE",
                public_steps=steps,
            )

        def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
            return None

        def get_evidence(
            self, investigation_id: str, evidence_id: str
        ) -> dict[str, Any] | None:
            return None

    inv_api.set_investigation_service(FailService())
    try:
        client = _client()
        response = client.post(
            "/workbench",
            data={"question": "What does E1001 mean?"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        body = response.text
        assert iid in body
        assert "理解目标" in body, "failure timeline missing zh label 理解目标"
        assert "检索 1/3" in body, "failure timeline missing zh label 检索 1/3"
    finally:
        inv_api.reset_investigation_service()


def test_workbench_answered_shows_nonempty_summary() -> None:
    """AC-016 UI: ANSWERED must display human-readable nonempty output.summary."""
    from support_platform.api import investigations as inv_api

    summary = "PHASE3_ANSWERED_SUMMARY_UNIQUE_S21"
    svc = _ok_service(
        result_status="ANSWERED",
        summary=summary,
        steps=[{"kind": "synthesize", "label": "生成建议"}],
    )
    inv_api.set_investigation_service(svc)
    try:
        client = _client()
        response = client.post(
            "/workbench",
            data={"question": "How do I reset my password?"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        body = response.text
        assert "ANSWERED" in body
        assert summary in body, "ANSWERED suggestion area must show output.summary"
        assert 'id="suggestion-text"' in body or 'class="suggestion"' in body
    finally:
        inv_api.reset_investigation_service()


def test_workbench_conflict_shows_nonempty_summary() -> None:
    """AC-016 UI: CONFLICT must display nonempty summary outlining the conflict."""
    from support_platform.api import investigations as inv_api

    summary = "冲突：一方要求 MFA，另一方称从不需要 MFA。PHASE3_CONFLICT_S21"
    svc = _ok_service(
        result_status="CONFLICT",
        summary=summary,
        steps=[
            {"kind": "compare_evidence", "label": "比较证据"},
            {"kind": "synthesize", "label": "生成建议"},
        ],
    )
    inv_api.set_investigation_service(svc)
    try:
        client = _client()
        response = client.post(
            "/workbench",
            data={"question": "Do I need MFA?"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        body = response.text
        assert "CONFLICT" in body
        assert summary in body, "CONFLICT suggestion area must show output.summary"
        assert 'id="suggestion-text"' in body or 'class="suggestion"' in body
    finally:
        inv_api.reset_investigation_service()


def test_workbench_js_step_event_prefers_public_label() -> None:
    """AC-015 / REQ-008: SSE STEP handler must prefer public_payload.label."""
    js_path = _STATIC / "workbench.js"
    assert js_path.is_file(), "static/workbench.js must exist"
    js = js_path.read_text(encoding="utf-8")
    # Prefer label for STEP timeline items (not kind/summary alone).
    assert "payload.label" in js or "data.label" in js or ".label" in js, (
        "workbench.js STEP handler must read public label from SSE payload"
    )
    # Narrower: label must appear near STEP handling, not only unrelated uses.
    step_idx = js.find('"STEP"')
    if step_idx < 0:
        step_idx = js.find("'STEP'")
    assert step_idx >= 0, "workbench.js must handle STEP events"
    window = js[max(0, step_idx - 200) : step_idx + 800]
    assert "label" in window, (
        "STEP handling window must reference label for timeline rendering"
    )


def test_workbench_dom_omits_sensitive_step_fields() -> None:
    """AC-015: cot/prompt/api_key/internal must not appear in visible step DOM."""
    from support_platform.api import investigations as inv_api

    secret_cot = "HIDDEN_COT_MUST_NOT_RENDER_S21"
    secret_prompt = "SYSTEM_PROMPT_MUST_NOT_RENDER_S21"
    steps = [
        {
            "kind": "synthesize",
            "label": "生成建议",
            "cot": secret_cot,
            "prompt": secret_prompt,
            "api_key": "sk-fake-not-for-dom",
            "internal": "INTERNAL_TRACE_S21",
        }
    ]
    svc = _ok_service(
        result_status="ANSWERED",
        summary="可见建议正文。",
        steps=steps,
    )
    inv_api.set_investigation_service(svc)
    try:
        client = _client()
        response = client.post(
            "/workbench",
            data={"question": "How do I reset my password?"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        body = response.text
        for leak in (secret_cot, secret_prompt, "sk-fake-not-for-dom", "INTERNAL_TRACE_S21"):
            assert leak not in body, f"sensitive field leaked into DOM: {leak!r}"
        # Safe label must render once timeline uses public_steps.label.
        assert "生成建议" in body, "safe zh label must still render"
        # Template must not iterate raw step keys into the page.
        tpl = (_TEMPLATES / "workbench.html").read_text(encoding="utf-8")
        for key in _FORBIDDEN:
            assert f"step.{key}" not in tpl and f"step['{key}']" not in tpl
    finally:
        inv_api.reset_investigation_service()
