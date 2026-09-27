"""S6 API / feedback RED contracts (REQ-005 / AC-006 API segment; SPEC §4 OP-01～04)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from support_platform.main import app

    return TestClient(app)


def _load_database_url() -> str:
    """Resolve DATABASE_URL from process env or repo .env (same convention as smoke)."""
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
    raise AssertionError("DATABASE_URL must be set for OP-04 PG persistence contract")


def _seed_completed_investigation(*, database_url: str, investigation_id: str, output: dict[str, Any]) -> None:
    """Insert a COMPLETED investigation row so investigation_feedback FK can succeed."""
    import json

    from sqlalchemy import create_engine, text

    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM investigation_feedback WHERE investigation_id = CAST(:id AS uuid)"),
            {"id": investigation_id},
        )
        conn.execute(
            text("DELETE FROM investigations WHERE id = CAST(:id AS uuid)"),
            {"id": investigation_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO investigations (
                  id, question, context, active_version_ids, task_status,
                  result_status, output, public_steps, error_code, config_hash
                ) VALUES (
                  CAST(:id AS uuid), :q, CAST(:ctx AS jsonb), CAST(:avs AS jsonb),
                  'COMPLETED', 'ANSWERED', CAST(:out AS jsonb), CAST(:steps AS jsonb),
                  NULL, NULL
                )
                """
            ),
            {
                "id": investigation_id,
                "q": "S6-RED feedback persistence seed",
                "ctx": "{}",
                "avs": "[]",
                "out": json.dumps(output, ensure_ascii=False),
                "steps": "[]",
            },
        )


def _count_feedback_rows(*, database_url: str, investigation_id: str) -> int:
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM investigation_feedback
                    WHERE investigation_id = CAST(:id AS uuid)
                    """
                ),
                {"id": investigation_id},
            ).scalar_one()
        )


def _fetch_feedback_actions(*, database_url: str, investigation_id: str) -> list[str]:
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT action FROM investigation_feedback
                WHERE investigation_id = CAST(:id AS uuid)
                ORDER BY created_at ASC
                """
            ),
            {"id": investigation_id},
        ).fetchall()
    return [str(r[0]) for r in rows]


def _cleanup_investigation(*, database_url: str, investigation_id: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM investigation_feedback WHERE investigation_id = CAST(:id AS uuid)"),
            {"id": investigation_id},
        )
        conn.execute(
            text("DELETE FROM investigations WHERE id = CAST(:id AS uuid)"),
            {"id": investigation_id},
        )


def test_empty_question_returns_422() -> None:
    client = _client()
    response = client.post("/api/investigations", json={"question": ""})
    assert response.status_code == 422


def test_whitespace_only_question_returns_422() -> None:
    client = _client()
    response = client.post("/api/investigations", json={"question": "   "})
    assert response.status_code == 422


def test_no_available_knowledge_returns_409() -> None:
    """OP-01: when service reports no usable knowledge, return 409 (not a fake success)."""
    from support_platform.api import investigations as inv_api

    class NoKnowledgeService:
        def create_investigation(self, *, question: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
            raise inv_api.NoAvailableKnowledgeError("no READY knowledge")

    inv_api.set_investigation_service(NoKnowledgeService())
    try:
        client = _client()
        response = client.post(
            "/api/investigations",
            json={"question": "How do I reset my password?"},
        )
        assert response.status_code == 409
        body = response.json()
        assert body.get("task_status") != "COMPLETED" or "result_status" not in body or body.get(
            "result_status"
        ) != "ANSWERED"
    finally:
        inv_api.reset_investigation_service()


def test_dependency_failure_after_create_returns_503_with_id_and_steps() -> None:
    """OP-01: after investigation row exists, dependency failure → 503 FAILED + id + public_steps."""
    from support_platform.api import investigations as inv_api

    iid = str(uuid4())
    steps = [{"kind": "retrieve_1", "hit_count": 0}]

    class FailAfterCreateService:
        def create_investigation(self, *, question: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
            raise inv_api.InvestigationDependencyError(
                investigation_id=iid,
                error_code="CHAT_UNAVAILABLE",
                public_steps=steps,
            )

    inv_api.set_investigation_service(FailAfterCreateService())
    try:
        client = _client()
        response = client.post(
            "/api/investigations",
            json={"question": "What does E1001 mean?"},
        )
        assert response.status_code == 503
        body = response.json()
        assert body["id"] == iid
        assert body["task_status"] == "FAILED"
        assert body.get("error_code") == "CHAT_UNAVAILABLE"
        assert isinstance(body.get("public_steps"), list)
        assert body["public_steps"] == steps
        # Must not look like a successful business answer.
        assert body.get("result_status") in (None, "NULL") or body.get("result_status") is None
    finally:
        inv_api.reset_investigation_service()


def test_get_unknown_investigation_returns_404() -> None:
    client = _client()
    response = client.get(f"/api/investigations/{uuid4()}")
    assert response.status_code == 404
    body = response.json()
    # Must be the investigation resource 404, not a missing-route shell.
    detail = str(body.get("detail") or body.get("error_code") or body).lower()
    assert "investigation" in detail or body.get("error_code") == "INVESTIGATION_NOT_FOUND"


def test_get_evidence_not_belonging_returns_404() -> None:
    from support_platform.api import investigations as inv_api

    iid = str(uuid4())

    class StoreService:
        def create_investigation(self, *, question: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "id": iid,
                "task_status": "COMPLETED",
                "result_status": "NO_EVIDENCE",
                "public_steps": [],
                "claims": [],
                "evidence": [],
                "output": {"summary": "no evidence"},
            }

        def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
            if investigation_id == iid:
                return self.create_investigation(question="x")
            return None

        def get_evidence(self, investigation_id: str, evidence_id: str) -> dict[str, Any] | None:
            return None

    inv_api.set_investigation_service(StoreService())
    try:
        client = _client()
        created = client.post("/api/investigations", json={"question": "orphan evidence lookup"})
        assert created.status_code == 200
        response = client.get(f"/api/investigations/{iid}/evidence/{uuid4()}")
        assert response.status_code == 404
    finally:
        inv_api.reset_investigation_service()


def test_feedback_appends_without_overwriting_ai_output() -> None:
    from support_platform.api import investigations as inv_api
    from support_platform.feedback import store as fb_store

    iid = str(uuid4())
    original_output = {"summary": "AI original reply must survive", "claims": []}

    class CompletedService:
        def __init__(self) -> None:
            self.row = {
                "id": iid,
                "task_status": "COMPLETED",
                "result_status": "ANSWERED",
                "public_steps": [{"kind": "retrieve_1"}],
                "claims": [{"text": "fact", "type": "FACT", "evidence_ids": []}],
                "evidence": [],
                "output": dict(original_output),
            }

        def create_investigation(self, *, question: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
            return dict(self.row)

        def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
            return dict(self.row) if investigation_id == iid else None

    inv_api.set_investigation_service(CompletedService())
    fb_store.reset()
    try:
        client = _client()
        created = client.post("/api/investigations", json={"question": "reset password steps"})
        assert created.status_code == 200
        ai_before = created.json().get("output") or created.json().get("summary")
        fb = client.post(
            f"/api/investigations/{iid}/feedback",
            json={"action": "EDITED", "reason": "tweak tone", "edited_text": "agent rewrite"},
        )
        assert fb.status_code == 200
        body = fb.json()
        assert "feedback_id" in body
        assert body["action"] == "EDITED"

        again = client.get(f"/api/investigations/{iid}")
        assert again.status_code == 200
        got = again.json()
        # AI original must remain; feedback is append-only.
        assert (got.get("output") or {}) == original_output or got.get("output") == original_output
        assert got.get("output") != {"summary": "agent rewrite"}
        assert ai_before is not None
    finally:
        inv_api.reset_investigation_service()
        fb_store.reset()


def test_feedback_on_failed_investigation_returns_409() -> None:
    from support_platform.api import investigations as inv_api

    iid = str(uuid4())

    class FailedService:
        def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
            if investigation_id != iid:
                return None
            return {
                "id": iid,
                "task_status": "FAILED",
                "result_status": None,
                "public_steps": [{"kind": "retrieve_1"}],
                "error_code": "CHAT_UNAVAILABLE",
                "output": None,
            }

        def create_investigation(self, *, question: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
            raise AssertionError("not used")

    inv_api.set_investigation_service(FailedService())
    try:
        client = _client()
        response = client.post(
            f"/api/investigations/{iid}/feedback",
            json={"action": "ADOPTED"},
        )
        assert response.status_code == 409
    finally:
        inv_api.reset_investigation_service()


def test_invalid_feedback_action_returns_422() -> None:
    from support_platform.api import investigations as inv_api

    iid = str(uuid4())

    class CompletedService:
        def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
            return {
                "id": iid,
                "task_status": "COMPLETED",
                "result_status": "ANSWERED",
                "public_steps": [],
                "output": {"summary": "ok"},
            }

    inv_api.set_investigation_service(CompletedService())
    try:
        client = _client()
        response = client.post(
            f"/api/investigations/{iid}/feedback",
            json={"action": "DELETED"},
        )
        assert response.status_code == 422
    finally:
        inv_api.reset_investigation_service()


def test_feedback_persists_to_investigation_feedback_pg_without_overwriting_ai() -> None:
    """OP-04 / SPEC §3§7: feedback must INSERT into investigation_feedback (real PG), not memory-only."""
    from support_platform.api import investigations as inv_api
    from support_platform.feedback import store as fb_store

    database_url = _load_database_url()
    iid = str(uuid4())
    original_output = {"summary": "AI original must survive PG feedback", "claims": []}

    class CompletedService:
        def __init__(self) -> None:
            self.row = {
                "id": iid,
                "task_status": "COMPLETED",
                "result_status": "ANSWERED",
                "public_steps": [{"kind": "retrieve_1"}],
                "claims": [],
                "evidence": [],
                "output": dict(original_output),
            }

        def create_investigation(self, *, question: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
            return dict(self.row)

        def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
            return dict(self.row) if investigation_id == iid else None

    _seed_completed_investigation(
        database_url=database_url, investigation_id=iid, output=original_output
    )
    inv_api.set_investigation_service(CompletedService())
    fb_store.reset()
    try:
        assert _count_feedback_rows(database_url=database_url, investigation_id=iid) == 0

        client = _client()
        fb = client.post(
            f"/api/investigations/{iid}/feedback",
            json={
                "action": "ADOPTED",
                "reason": "s6-red pg persist",
                "edited_text": None,
            },
        )
        assert fb.status_code == 200
        body = fb.json()
        assert "feedback_id" in body
        assert body["action"] == "ADOPTED"

        # Durable side effect: row must exist in investigation_feedback (not only process memory).
        actions = _fetch_feedback_actions(database_url=database_url, investigation_id=iid)
        assert actions == ["ADOPTED"], (
            "OP-04 must persist feedback into PostgreSQL investigation_feedback; "
            f"got PG actions={actions!r} (memory-only store is not SPEC §3/§7 compliant)"
        )
        assert _count_feedback_rows(database_url=database_url, investigation_id=iid) == 1

        # Clearing process-local memory must not erase the durable record.
        fb_store.reset()
        assert _fetch_feedback_actions(database_url=database_url, investigation_id=iid) == [
            "ADOPTED"
        ]

        again = client.get(f"/api/investigations/{iid}")
        assert again.status_code == 200
        assert again.json().get("output") == original_output
    finally:
        inv_api.reset_investigation_service()
        fb_store.reset()
        _cleanup_investigation(database_url=database_url, investigation_id=iid)
