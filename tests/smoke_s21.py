"""S21 real-path SMOKE: uvicorn workbench → process timeline labels + summary.

Official entry: uvicorn support_platform.main:app → GET/POST /
Requires real Chat + Embedding + PostgreSQL. Fake adapters refused.
Paid provider calls require explicit user approval for this Step.
"""

from __future__ import annotations

import re
import sys

from fastapi.testclient import TestClient

from support_platform.api import investigations as inv_api
from support_platform.api.real_service import RealInvestigationService
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

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")
_FORBIDDEN_BLOB = (
    "hidden_cot",
    "system prompt",
    "chain of thought",
)
_API_KEY_RE = re.compile(r"sk-[a-zA-Z0-9]{8,}")


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
    print("official_entry", "uvicorn support_platform.main:app → GET/POST / workbench")
    print("also_static", "GET /static/workbench.js (STEP prefers payload.label)")

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

    client = TestClient(app)
    try:
        home = client.get("/")
        if home.status_code != 200 or "text/html" not in (
            home.headers.get("content-type") or ""
        ):
            print("SMOKE_FAIL workbench get", home.status_code, file=sys.stderr)
            return 1
        if 'name="question"' not in home.text:
            print("SMOKE_FAIL workbench form missing", file=sys.stderr)
            return 1
        js = client.get("/static/workbench.js")
        if js.status_code != 200 or "payload.label" not in js.text:
            print("SMOKE_FAIL workbench.js missing payload.label", file=sys.stderr)
            return 1
        print("workbench_get_ok")

        created = client.post(
            "/",
            data={
                "question": "How do I reset my password? Also what does error E1001 mean?",
                "error_code": "E1001",
            },
            follow_redirects=True,
        )
        if created.status_code != 200:
            print(
                "SMOKE_FAIL submit",
                created.status_code,
                created.text[:300],
                file=sys.stderr,
            )
            return 1
        body = created.text
        if 'data-task-status="COMPLETED"' not in body and "ANSWERED" not in body and "CONFLICT" not in body and "NO_EVIDENCE" not in body and "NEEDS_HUMAN" not in body:
            print("SMOKE_FAIL no result panel", file=sys.stderr)
            print(body[:800], file=sys.stderr)
            return 1

        # AC-015: Chinese process labels visible in timeline.
        if not _ZH_RE.search(body):
            print("SMOKE_FAIL no Chinese labels in HTML", file=sys.stderr)
            return 1
        label_hits = 0
        for token in ("理解", "检索", "比较", "生成", "拆分"):
            if token in body:
                label_hits += 1
        if label_hits < 2:
            print(
                "SMOKE_FAIL timeline labels insufficient",
                label_hits,
                file=sys.stderr,
            )
            return 1

        # AC-016: suggestion area nonempty when ANSWERED/CONFLICT.
        answered_or_conflict = "ANSWERED" in body or "CONFLICT" in body
        if answered_or_conflict:
            if 'id="suggestion-text"' not in body and 'class="suggestion"' not in body:
                print("SMOKE_FAIL suggestion area missing", file=sys.stderr)
                return 1
            # Extract suggestion block roughly.
            m = re.search(
                r'id="suggestion-text"[^>]*>(.*?)</div>',
                body,
                re.S | re.I,
            )
            suggestion = (m.group(1) if m else "").strip()
            suggestion = re.sub(r"<[^>]+>", "", suggestion).strip()
            if not suggestion or suggestion.startswith("（无"):
                print("SMOKE_FAIL empty summary for ANSWERED/CONFLICT", file=sys.stderr)
                return 1
            print("summary_ok", len(suggestion))
        else:
            print("summary_optional_status", "non-ANSWERED/CONFLICT")

        for forbidden in _FORBIDDEN_BLOB:
            if forbidden in body.lower():
                print("SMOKE_FAIL leak", forbidden, file=sys.stderr)
                return 1
        # Avoid false positive on HTML id "progress-task-status" (contains "sk-").
        if _API_KEY_RE.search(body) or re.search(r"\bapi_key\b", body, re.I):
            print("SMOKE_FAIL leak api_key", file=sys.stderr)
            return 1

        print("timeline_labels_ok", "hits", label_hits)
        print("workbench_submit_ok")
    finally:
        inv_api.reset_investigation_service()

    print("no_fake", "HttpChatAdapter+HttpEmbeddingAdapter+PostgreSQL+TestClient(main:app)")
    print("S21_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
