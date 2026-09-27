"""S7 workbench UI RED contracts (REQ-005 / AC-006; SPEC §2 templates + §4 page reuses OP-01～04)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from support_platform.main import app

    return TestClient(app)


def test_workbench_get_returns_html_form() -> None:
    """Local workbench entry must serve an HTML form for question (+ optional context)."""
    client = _client()
    response = client.get("/workbench")
    assert response.status_code == 200
    ctype = (response.headers.get("content-type") or "").lower()
    assert "text/html" in ctype
    body = response.text
    assert 'name="question"' in body or "name='question'" in body
    # Optional context fields for product_version / error_code per OP-01.
    assert "error_code" in body or "product_version" in body or "context" in body
    assert 'type="submit"' in body or "submit" in body.lower()


def test_workbench_static_stylesheet_is_linked() -> None:
    """Native CSS must be served (SPEC §1 FastAPI + Jinja2 + native CSS)."""
    client = _client()
    page = client.get("/workbench")
    assert page.status_code == 200
    html = page.text
    assert "/static/" in html or 'rel="stylesheet"' in html
    # At least one stylesheet asset must resolve.
    css = client.get("/static/workbench.css")
    assert css.status_code == 200
    assert "text/css" in (css.headers.get("content-type") or "").lower() or css.text.strip() != ""


def test_workbench_submit_shows_result_status_steps_and_suggestion() -> None:
    """After submit, page shows result_status, public steps, and suggestion (not raw JSON-only)."""
    from support_platform.api import investigations as inv_api

    iid = str(uuid4())
    eid = str(uuid4())

    class OkService:
        def create_investigation(
            self, *, question: str, context: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return {
                "id": iid,
                "task_status": "COMPLETED",
                "result_status": "ANSWERED",
                "public_steps": [{"kind": "retrieve_1", "hit_count": 2}],
                "claims": [{"text": "Reset via Settings", "type": "FACT", "evidence_ids": [eid]}],
                "evidence": [
                    {
                        "id": eid,
                        "quote_snapshot": "Open Settings > Security to reset password.",
                        "source_title_snapshot": "FAQ",
                    }
                ],
                "output": {"summary": "Reset password from Settings > Security."},
            }

        def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
            if investigation_id != iid:
                return None
            return self.create_investigation(question="x")

        def get_evidence(
            self, investigation_id: str, evidence_id: str
        ) -> dict[str, Any] | None:
            if investigation_id == iid and evidence_id == eid:
                return {
                    "id": eid,
                    "quote_snapshot": "Open Settings > Security to reset password.",
                    "source_title_snapshot": "FAQ",
                    "locator_snapshot": {"kind": "md", "line_start": 1, "line_end": 2},
                }
            return None

    inv_api.set_investigation_service(OkService())
    try:
        client = _client()
        response = client.post(
            "/workbench",
            data={"question": "How do I reset my password?", "error_code": "E1001"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        body = response.text
        assert "text/html" in (response.headers.get("content-type") or "").lower()
        assert "ANSWERED" in body
        assert "retrieve_1" in body or "public" in body.lower() or "步骤" in body
        assert "Reset password" in body or "Settings" in body
        # Citation / open-source link toward OP-03.
        assert eid in body or f"/evidence/" in body or "quote" in body.lower() or "引用" in body
    finally:
        inv_api.reset_investigation_service()


def test_workbench_can_open_evidence_quote() -> None:
    """Opening a citation shows quote snapshot (OP-03 surfaced in UI)."""
    from support_platform.api import investigations as inv_api

    iid = str(uuid4())
    eid = str(uuid4())
    quote = "UNIQUE_QUOTE_SNAPSHOT_S7_RED"

    class StoreService:
        def create_investigation(
            self, *, question: str, context: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            raise AssertionError("not used")

        def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
            if investigation_id != iid:
                return None
            return {
                "id": iid,
                "task_status": "COMPLETED",
                "result_status": "ANSWERED",
                "public_steps": [],
                "output": {"summary": "ok"},
                "evidence": [{"id": eid}],
            }

        def get_evidence(
            self, investigation_id: str, evidence_id: str
        ) -> dict[str, Any] | None:
            if investigation_id == iid and evidence_id == eid:
                return {
                    "id": eid,
                    "quote_snapshot": quote,
                    "source_title_snapshot": "Guide",
                    "locator_snapshot": {"kind": "md", "line_start": 3, "line_end": 4},
                }
            return None

    inv_api.set_investigation_service(StoreService())
    try:
        client = _client()
        response = client.get(f"/investigations/{iid}/evidence/{eid}")
        assert response.status_code == 200
        assert "text/html" in (response.headers.get("content-type") or "").lower()
        assert quote in response.text
    finally:
        inv_api.reset_investigation_service()


def test_workbench_dependency_failure_shows_id_steps_and_retry_not_success() -> None:
    """AC-006: dependency failure shows failure segment + retry, not a success result."""
    from support_platform.api import investigations as inv_api

    iid = str(uuid4())
    steps = [{"kind": "retrieve_1", "hit_count": 0}]

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
        assert "CHAT_UNAVAILABLE" in body or "FAILED" in body or "失败" in body
        assert "retrieve_1" in body or "步骤" in body
        assert "重试" in body or "retry" in body.lower()
        # Must not present as a successful answered investigation.
        assert "ANSWERED" not in body
        assert 'data-task-status="COMPLETED"' not in body
    finally:
        inv_api.reset_investigation_service()


def test_workbench_feedback_controls_present_after_completed_result() -> None:
    """Feedback ADOPTED / EDITED / ESCALATED controls must be available on completed result."""
    from support_platform.api import investigations as inv_api

    iid = str(uuid4())

    class OkService:
        def create_investigation(
            self, *, question: str, context: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return {
                "id": iid,
                "task_status": "COMPLETED",
                "result_status": "NO_EVIDENCE",
                "public_steps": [{"kind": "retrieve_1"}],
                "claims": [],
                "evidence": [],
                "output": {"summary": "No supporting evidence found."},
            }

        def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
            return self.create_investigation(question="x") if investigation_id == iid else None

        def get_evidence(
            self, investigation_id: str, evidence_id: str
        ) -> dict[str, Any] | None:
            return None

    inv_api.set_investigation_service(OkService())
    try:
        client = _client()
        response = client.post(
            "/workbench",
            data={"question": "Is feature X supported on plan Free?"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        body = response.text
        for action in ("ADOPTED", "EDITED", "ESCALATED"):
            assert action in body or action.lower() in body or (
                {"ADOPTED": "采纳", "EDITED": "修改", "ESCALATED": "转人工"}[action] in body
            )
    finally:
        inv_api.reset_investigation_service()


def test_workbench_submit_disables_duplicate_clicks() -> None:
    """SPEC OP-01: frontend must disable duplicate submit (attribute or script hook)."""
    client = _client()
    response = client.get("/workbench")
    assert response.status_code == 200
    body = response.text
    markers = (
        "disabled",
        "data-disable-on-submit",
        "aria-busy",
        "once",
        "submitting",
        "preventDefault",
    )
    assert any(m in body for m in markers) or "addEventListener" in body
