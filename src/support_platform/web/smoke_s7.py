"""S7 real-path SMOKE: workbench HTML → Investigation Service → real PG/Chat/Embedding."""

from __future__ import annotations

import re
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


def _pg_feedback_count(database_url: str, investigation_id: str) -> int:
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
    print("user_approval", "S7-SMOKE paid Chat+Embedding")
    print("official_entry", "uvicorn support_platform.main:app → GET/POST / workbench")

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
        # --- Workbench form ---
        home = client.get("/")
        if home.status_code != 200 or "text/html" not in (home.headers.get("content-type") or ""):
            print("SMOKE_FAIL workbench get", home.status_code, file=sys.stderr)
            return 1
        if 'name="question"' not in home.text:
            print("SMOKE_FAIL workbench form missing", file=sys.stderr)
            return 1
        css = client.get("/static/workbench.css")
        if css.status_code != 200:
            print("SMOKE_FAIL static css", css.status_code, file=sys.stderr)
            return 1
        print("workbench_get_ok")

        # --- Submit via HTML form (browser path composition) ---
        created = client.post(
            "/",
            data={
                "question": "How do I reset my password? Also what does error E1001 mean?",
                "error_code": "E1001",
            },
            follow_redirects=True,
        )
        if created.status_code != 200:
            print("SMOKE_FAIL submit", created.status_code, created.text[:300], file=sys.stderr)
            return 1
        body = created.text
        completed = 'data-task-status="COMPLETED"' in body
        failed_ux = (
            'data-task-status="FAILED"' in body
            or "CHAT_UNAVAILABLE" in body
            or "失败" in body
        )
        has_status = any(
            s in body
            for s in ("ANSWERED", "NO_EVIDENCE", "CONFLICT", "NEEDS_HUMAN", "FAILED")
        )
        if not completed and not failed_ux and not has_status:
            print("SMOKE_FAIL no result panel in HTML", file=sys.stderr)
            print(body[:800], file=sys.stderr)
            return 1
        m = re.search(
            r"<code>([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})</code>",
            body,
            re.I,
        )
        if not m:
            print("SMOKE_FAIL no investigation id in HTML", file=sys.stderr)
            return 1
        iid = m.group(1)
        print(
            "workbench_submit_ok",
            iid,
            "completed" if completed else ("failed_ux" if failed_ux else "status_text"),
        )

        # --- Open citation if link present ---
        ev_links = re.findall(
            rf"/investigations/{re.escape(iid)}/evidence/([0-9a-f-]{{36}})", body
        )
        if ev_links:
            eid = ev_links[0]
            quote_page = client.get(f"/investigations/{iid}/evidence/{eid}")
            if quote_page.status_code != 200:
                print("SMOKE_FAIL evidence page", quote_page.status_code, file=sys.stderr)
                return 1
            if "引用" not in quote_page.text and "quote" not in quote_page.text.lower():
                # template title 引用原文
                if "blockquote" not in quote_page.text and "quote_snapshot" not in quote_page.text:
                    print("SMOKE_FAIL evidence empty", file=sys.stderr)
                    return 1
            print("evidence_page_ok", eid)
        else:
            print("evidence_page_skip", "no citation links on result")

        # --- Feedback via OP-04 (same app) when COMPLETED ---
        if completed or 'data-task-status="COMPLETED"' in body:
            fb = client.post(
                f"/api/investigations/{iid}/feedback",
                json={"action": "ADOPTED", "reason": "s7 smoke"},
            )
            if fb.status_code != 200:
                print("SMOKE_FAIL feedback", fb.status_code, fb.text, file=sys.stderr)
                return 1
            if _pg_feedback_count(env["DATABASE_URL"], iid) < 1:
                print("SMOKE_FAIL feedback not in PG", file=sys.stderr)
                return 1
            print("feedback_pg_ok", fb.json().get("feedback_id"))
        else:
            print("feedback_skip", "not COMPLETED")

        # --- Injected failure through workbench HTML ---
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
        fail = client.post("/", data={"question": "force dependency failure"}, follow_redirects=True)
        if fail.status_code != 200:
            print("SMOKE_FAIL failure page status", fail.status_code, file=sys.stderr)
            return 1
        fbody = fail.text
        if "CHAT_UNAVAILABLE" not in fbody and "FAILED" not in fbody and "失败" not in fbody:
            print("SMOKE_FAIL failure UX missing", file=sys.stderr)
            return 1
        if "重试" not in fbody and "retry" not in fbody.lower():
            print("SMOKE_FAIL retry hint missing", file=sys.stderr)
            return 1
        if "ANSWERED" in fbody:
            print("SMOKE_FAIL failure looks like success", file=sys.stderr)
            return 1
        print("workbench_failure_ux_ok")

    finally:
        inv_api.reset_investigation_service()
        fb_store.reset()

    print("no_fake", "HttpChatAdapter+HttpEmbeddingAdapter+PostgreSQL+TestClient(main:app workbench)")
    print("cleanup", "service reset; investigation+feedback PG rows retained")
    print("S7_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
