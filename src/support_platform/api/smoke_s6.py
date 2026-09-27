"""S6 real-path SMOKE: HTTP OP-01～04 → Investigation Service → real PG/Chat/Embedding."""

from __future__ import annotations

import sys
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from support_platform.api import investigations as inv_api
from support_platform.api.real_service import RealInvestigationService
from support_platform.feedback import store as fb_store
from support_platform.infrastructure.chat import HttpChatAdapter
from support_platform.infrastructure.embedding import HttpEmbeddingAdapter
from support_platform.investigation import fail_stale_running
from support_platform.investigation.pg_store import PgInvestigationStore
from support_platform.investigation.smoke_s4 import (
    _ensure_corpus,
    _make_retrieve,
    require_real_chat_env,
)
from support_platform.main import app


def _pg_feedback_actions(database_url: str, investigation_id: str) -> list[str]:
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


def main() -> int:
    try:
        env = require_real_chat_env()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print("llm_provider", "dashscope-http")
    print("llm_model", env["LLM_MODEL"])
    print("embedding_model", env["EMBEDDING_MODEL"])
    print("database", env["DATABASE_URL"].split("@")[-1])
    print("user_approval", "S6-SMOKE paid Chat+Embedding")

    try:
        _ensure_corpus(env["DATABASE_URL"])
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL corpus", exc, file=sys.stderr)
        return 1

    store = PgInvestigationStore(env["DATABASE_URL"])
    cleaned = fail_stale_running(store)
    print("stale_cleanup_startup", len(cleaned))

    embedding = HttpEmbeddingAdapter(
        base_url=env["EMBEDDING_BASE_URL"],
        api_key=env["EMBEDDING_API_KEY"],
        model=env["EMBEDDING_MODEL"],
        dimension=int(env["EMBEDDING_DIMENSION"]),
    )
    chat = HttpChatAdapter(
        base_url=env["LLM_BASE_URL"],
        api_key=env["LLM_API_KEY"],
        model=env["LLM_MODEL"],
    )
    if type(chat).__name__ == "FakeChatAdapter":
        print("SMOKE_FAIL Fake chat", file=sys.stderr)
        return 1

    retrieve = _make_retrieve(env, embedding)
    real = RealInvestigationService(
        database_url=env["DATABASE_URL"],
        store=store,
        retrieve=retrieve,
        chat=chat,
    )
    inv_api.set_investigation_service(real)
    fb_store.reset()

    client = TestClient(app)
    try:
        # --- OP-01 happy path (real) ---
        created = client.post(
            "/api/investigations",
            json={
                "question": "How do I reset my password? Also what does error E1001 mean?",
                "context": {"error_code": "E1001"},
            },
        )
        if created.status_code != 200:
            print("SMOKE_FAIL create", created.status_code, created.text, file=sys.stderr)
            return 1
        body = created.json()
        iid = body["id"]
        if body.get("task_status") not in {"COMPLETED", "FAILED"}:
            print("SMOKE_FAIL task_status", body.get("task_status"), file=sys.stderr)
            return 1
        if not isinstance(body.get("public_steps"), list):
            print("SMOKE_FAIL public_steps", file=sys.stderr)
            return 1
        print(
            "op01_create_ok",
            iid,
            body["task_status"],
            "result",
            body.get("result_status"),
            "steps",
            len(body["public_steps"]),
        )
        original_output = body.get("output")

        # --- OP-02 GET ---
        got = client.get(f"/api/investigations/{iid}")
        if got.status_code != 200:
            print("SMOKE_FAIL get", got.status_code, got.text, file=sys.stderr)
            return 1
        print("op02_get_ok", got.json()["id"])

        # --- OP-03 missing evidence ---
        miss = client.get(f"/api/investigations/{iid}/evidence/{uuid4()}")
        if miss.status_code != 404:
            print("SMOKE_FAIL evidence 404", miss.status_code, file=sys.stderr)
            return 1
        print("op03_evidence_404_ok")

        # --- OP-04 feedback append (only if COMPLETED) ---
        if body["task_status"] == "COMPLETED":
            fb = client.post(
                f"/api/investigations/{iid}/feedback",
                json={"action": "ADOPTED", "reason": "s6 smoke"},
            )
            if fb.status_code != 200:
                print("SMOKE_FAIL feedback", fb.status_code, fb.text, file=sys.stderr)
                return 1
            fb_body = fb.json()
            if "feedback_id" not in fb_body or fb_body.get("action") != "ADOPTED":
                print("SMOKE_FAIL feedback body", fb_body, file=sys.stderr)
                return 1
            again = client.get(f"/api/investigations/{iid}")
            if again.status_code != 200:
                print("SMOKE_FAIL get after feedback", file=sys.stderr)
                return 1
            if again.json().get("output") != original_output:
                print("SMOKE_FAIL output overwritten", file=sys.stderr)
                return 1
            listed = fb_store.list_for(iid)
            if not listed:
                print("SMOKE_FAIL feedback not stored", file=sys.stderr)
                return 1
            # Durable truth: SPEC §3 investigation_feedback (not memory-only).
            pg_actions = _pg_feedback_actions(env["DATABASE_URL"], iid)
            if pg_actions != ["ADOPTED"]:
                print(
                    "SMOKE_FAIL feedback not in investigation_feedback",
                    pg_actions,
                    file=sys.stderr,
                )
                return 1
            fb_store.reset()
            if _pg_feedback_actions(env["DATABASE_URL"], iid) != ["ADOPTED"]:
                print("SMOKE_FAIL PG feedback lost after memory reset", file=sys.stderr)
                return 1
            print(
                "op04_feedback_ok",
                fb_body["feedback_id"],
                "memory_rows",
                len(listed),
                "pg_actions",
                pg_actions,
            )
        else:
            print("op04_feedback_skip", "investigation not COMPLETED")

        # --- Injected failure paths (allowed) ---
        class FailSvc:
            def create_investigation(
                self, *, question: str, context: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                raise inv_api.InvestigationDependencyError(
                    investigation_id=str(uuid4()),
                    error_code="CHAT_UNAVAILABLE",
                    public_steps=[{"kind": "retrieve_1", "hit_count": 0}],
                )

            def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
                return None

            def get_evidence(
                self, investigation_id: str, evidence_id: str
            ) -> dict[str, Any] | None:
                return None

        inv_api.set_investigation_service(FailSvc())
        fail = client.post("/api/investigations", json={"question": "force dependency failure"})
        if fail.status_code != 503:
            print("SMOKE_FAIL expected 503", fail.status_code, file=sys.stderr)
            return 1
        fbody = fail.json()
        if fbody.get("task_status") != "FAILED" or "id" not in fbody:
            print("SMOKE_FAIL 503 body", fbody, file=sys.stderr)
            return 1
        print("injected_503_ok", fbody["id"], fbody.get("error_code"))

        class NoKnow:
            def create_investigation(
                self, *, question: str, context: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                raise inv_api.NoAvailableKnowledgeError("no READY knowledge")

            def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
                return None

            def get_evidence(
                self, investigation_id: str, evidence_id: str
            ) -> dict[str, Any] | None:
                return None

        inv_api.set_investigation_service(NoKnow())
        nk = client.post("/api/investigations", json={"question": "no knowledge path"})
        if nk.status_code != 409:
            print("SMOKE_FAIL expected 409", nk.status_code, file=sys.stderr)
            return 1
        print("injected_409_ok")

        empty = client.post("/api/investigations", json={"question": ""})
        if empty.status_code != 422:
            print("SMOKE_FAIL expected 422", empty.status_code, file=sys.stderr)
            return 1
        print("empty_422_ok")

    finally:
        inv_api.reset_investigation_service()
        fb_store.reset()

    print("official_entry", "python -m support_platform.api.smoke_s6")
    print("also_http", "uvicorn support_platform.main:app + POST /api/investigations")
    print("no_fake", "HttpChatAdapter+HttpEmbeddingAdapter+PostgreSQL+TestClient(app)")
    print(
        "cleanup",
        "feedback memory reset; investigation+feedback PG rows retained for audit",
    )
    print("S6_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
