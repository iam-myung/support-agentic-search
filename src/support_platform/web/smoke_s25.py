"""S25 real-path SMOKE: official GET / chat UI → OP-15/16/17 → real Chat+Web+PG.

Requires LLM_*/EMBEDDING_*/WEB_SEARCH_*/DATABASE_URL and explicit user approval
for this Step (paid Chat + paid Tavily). Fake adapters are refused.
Do not print API keys.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from support_platform.api import conversations as conv_api
from support_platform.infrastructure.chat import FakeChatAdapter, HttpChatAdapter
from support_platform.infrastructure.embedding import HttpEmbeddingAdapter
from support_platform.infrastructure.web_search import (
    FakeWebSearchAdapter,
    TavilyHttpWebSearchAdapter,
)
from support_platform.infrastructure.web_search.smoke_s23 import require_real_web_search_env
from support_platform.investigation.pg_store import PgInvestigationStore
from support_platform.investigation.smoke_s4 import (
    _ensure_corpus,
    _make_retrieve,
    require_real_chat_env,
)
from support_platform.investigation.smoke_s24 import _Phase4SmokePlanner
from support_platform.main import app

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")
_FORBIDDEN = frozenset({"prompt", "system", "cot", "api_key", "internal"})
_NON_LOCAL = (
    "非基于本地知识",
    "不是基于本地知识",
    "非本地知识库",
    "不基于本地知识库",
    "来自网络",
    "网络来源",
)


def main() -> int:
    try:
        chat_env = require_real_chat_env()
        web_env = require_real_web_search_env()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print("official_entry", "uvicorn → GET / chat UI → OP-15/16/17")
    print("llm_provider", "dashscope-http")
    print("llm_model", chat_env["LLM_MODEL"])
    print("web_search_provider", web_env["WEB_SEARCH_PROVIDER"])
    print("web_search_api_key", "SET")
    print("database", chat_env["DATABASE_URL"].split("@")[-1])

    try:
        _ensure_corpus(chat_env["DATABASE_URL"])
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL corpus", type(exc).__name__, file=sys.stderr)
        return 1

    embedding = HttpEmbeddingAdapter(
        base_url=chat_env["EMBEDDING_BASE_URL"],
        api_key=chat_env["EMBEDDING_API_KEY"],
        model=chat_env["EMBEDDING_MODEL"],
        dimension=int(chat_env["EMBEDDING_DIMENSION"]),
    )
    chat = HttpChatAdapter(
        base_url=chat_env["LLM_BASE_URL"],
        api_key=chat_env["LLM_API_KEY"],
        model=chat_env["LLM_MODEL"],
    )
    web = TavilyHttpWebSearchAdapter(
        base_url=web_env["WEB_SEARCH_BASE_URL"],
        api_key=web_env["WEB_SEARCH_API_KEY"],
        max_results=int(web_env["WEB_SEARCH_MAX_RESULTS"]),
        timeout_s=45.0,
    )
    if isinstance(chat, FakeChatAdapter) or type(chat).__name__ == "FakeChatAdapter":
        print("SMOKE_FAIL Fake chat", file=sys.stderr)
        return 1
    if isinstance(web, FakeWebSearchAdapter) or type(web).__name__ == "FakeWebSearchAdapter":
        print("SMOKE_FAIL Fake web_search", file=sys.stderr)
        return 1

    retrieve = _make_retrieve(chat_env, embedding)
    question = "How do I reset my password? Also check public MFA tips."
    planner = _Phase4SmokePlanner(chat=chat, question=question)

    conv_api.configure_conversation_turn(
        retrieve=retrieve,
        web_search=web,
        planner=planner,
        store_factory=lambda: PgInvestigationStore(chat_env["DATABASE_URL"]),
    )

    client = TestClient(app)
    try:
        root = client.get("/")
        if root.status_code != 200 or "text/html" not in (root.headers.get("content-type") or ""):
            print("SMOKE_FAIL GET /", root.status_code, file=sys.stderr)
            return 1
        html = root.text
        if "<h1>客服工作台</h1>" in html or 'id="investigate-form"' in html:
            print("SMOKE_FAIL legacy workbench still root", file=sys.stderr)
            return 1
        if "data-chat" not in html and 'id="chat"' not in html and "对话" not in html:
            print("SMOKE_FAIL not chat UI", file=sys.stderr)
            return 1
        if "chat.css" not in html or "chat.js" not in html:
            print("SMOKE_FAIL chat assets not linked", file=sys.stderr)
            return 1
        print("root_chat_ui", True)

        created = client.post("/api/v2/conversations", json={"title": "s25-smoke"})
        if created.status_code != 201:
            print("SMOKE_FAIL OP-15", created.status_code, file=sys.stderr)
            return 1
        cid = created.json().get("conversation_id") or created.json().get("id")
        if not cid:
            print("SMOKE_FAIL missing conversation_id", file=sys.stderr)
            return 1

        posted = client.post(
            f"/api/v2/conversations/{cid}/messages",
            json={"content": question},
            headers={"Idempotency-Key": str(uuid4())},
        )
        if posted.status_code not in {200, 202}:
            print("SMOKE_FAIL OP-16", posted.status_code, posted.text[:200], file=sys.stderr)
            return 1

        detail = client.get(f"/api/v2/conversations/{cid}")
        if detail.status_code != 200:
            print("SMOKE_FAIL OP-17", detail.status_code, file=sys.stderr)
            return 1
        body: dict[str, Any] = detail.json()
        steps = list(body.get("public_steps") or [])
        kinds = [str(s.get("kind") or "") for s in steps]
        if "local_retrieve" not in kinds or "web_search" not in kinds:
            print("SMOKE_FAIL tool steps", kinds, file=sys.stderr)
            return 1
        for step in steps:
            for bad in _FORBIDDEN:
                if bad in step:
                    print("SMOKE_FAIL forbidden", bad, file=sys.stderr)
                    return 1
            if step.get("kind") in {"local_retrieve", "web_search", "synthesize"}:
                label = step.get("label")
                if not isinstance(label, str) or not _ZH_RE.search(label or ""):
                    print("SMOKE_FAIL zh label", step, file=sys.stderr)
                    return 1

        output = body.get("output") or {}
        knowledge_basis = body.get("knowledge_basis") or output.get("knowledge_basis")
        summary = str(body.get("summary") or output.get("summary") or "")
        if knowledge_basis not in {"LOCAL", "WEB", "MIXED"}:
            print("SMOKE_FAIL knowledge_basis", knowledge_basis, file=sys.stderr)
            return 1
        if knowledge_basis in {"WEB", "MIXED"} and not any(m in summary for m in _NON_LOCAL):
            print("SMOKE_FAIL missing disclosure", file=sys.stderr)
            return 1
        if not summary.strip():
            print("SMOKE_FAIL empty summary", file=sys.stderr)
            return 1

        blob = json.dumps({"html": html, "detail": body}, ensure_ascii=False).lower()
        for secret in (
            chat_env["LLM_API_KEY"].lower(),
            web_env["WEB_SEARCH_API_KEY"].lower(),
            chat_env["EMBEDDING_API_KEY"].lower(),
        ):
            if secret and secret in blob:
                print("SMOKE_FAIL key leaked", file=sys.stderr)
                return 1

        print("conversation_id", cid)
        print("knowledge_basis", knowledge_basis)
        print("tool_steps", len([k for k in kinds if k in {"local_retrieve", "web_search"}]))
        print("summary_len", len(summary))
        print("public_steps_ok", True)
        print("no_fake", type(chat).__name__, type(web).__name__)
        print("S25_SMOKE_OK")
        return 0
    finally:
        conv_api.reset_conversation_turn()


if __name__ == "__main__":
    raise SystemExit(main())
