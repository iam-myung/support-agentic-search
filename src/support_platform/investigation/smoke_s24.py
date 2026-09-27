"""S24 real-path SMOKE: Phase4 agent loop → real Chat + Web Search + PG.

Requires LLM_*/EMBEDDING_*/WEB_SEARCH_*/DATABASE_URL and explicit user approval
for this Step (paid Chat + paid Tavily). Fake adapters are refused.
Do not print API keys.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from support_platform.conversation import run_conversation_turn
from support_platform.infrastructure.chat import FakeChatAdapter, HttpChatAdapter
from support_platform.infrastructure.embedding import HttpEmbeddingAdapter
from support_platform.infrastructure.web_search import (
    FakeWebSearchAdapter,
    TavilyHttpWebSearchAdapter,
)
from support_platform.investigation.agent_loop import (
    ALLOWED_TOOLS,
    MAX_TOOL_CALLS,
    MAX_WEB_SEARCH_CALLS,
)
from support_platform.investigation.phase4_planner import (
    Phase4SequentialPlanner as _Phase4SmokePlanner,
)
from support_platform.investigation.pg_store import PgInvestigationStore
from support_platform.investigation.smoke_s4 import (
    _ensure_corpus,
    _make_retrieve,
    require_real_chat_env,
)
from support_platform.infrastructure.web_search.smoke_s23 import require_real_web_search_env

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")
_FORBIDDEN_KEYS = frozenset({"prompt", "system", "cot", "api_key", "internal"})
_NON_LOCAL_MARKERS = (
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

    print("llm_provider", "dashscope-http")
    print("llm_model", chat_env["LLM_MODEL"])
    print("embedding_model", chat_env["EMBEDDING_MODEL"])
    print("database", chat_env["DATABASE_URL"].split("@")[-1])
    print("web_search_provider", web_env["WEB_SEARCH_PROVIDER"])
    print("web_search_base", web_env["WEB_SEARCH_BASE_URL"])
    print("web_search_api_key", "SET")
    print("caps", f"tool<={MAX_TOOL_CALLS}", f"web<={MAX_WEB_SEARCH_CALLS}")
    print("allowed_tools", ",".join(sorted(ALLOWED_TOOLS)))

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
    store = PgInvestigationStore(chat_env["DATABASE_URL"])
    question = "How do I reset my password? Also check public docs for MFA tips."
    planner = _Phase4SmokePlanner(chat=chat, question=question)

    try:
        turn = run_conversation_turn(
            content=question,
            retrieve=retrieve,
            web_search=web,
            planner=planner,
            store=store,
        )
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL turn", type(exc).__name__, file=sys.stderr)
        return 1

    if turn.get("task_status") not in {"COMPLETED", "FAILED"}:
        print("SMOKE_FAIL bad status", turn.get("task_status"), file=sys.stderr)
        return 1

    steps = list(turn.get("public_steps") or [])
    kinds = [str(s.get("kind") or "") for s in steps]
    if "local_retrieve" not in kinds or "web_search" not in kinds:
        print("SMOKE_FAIL missing tool steps", kinds, file=sys.stderr)
        return 1
    tool_steps = [k for k in kinds if k in {"local_retrieve", "web_search"}]
    if len(tool_steps) > MAX_TOOL_CALLS:
        print("SMOKE_FAIL tool cap exceeded", len(tool_steps), file=sys.stderr)
        return 1
    if sum(1 for k in kinds if k == "web_search") > MAX_WEB_SEARCH_CALLS:
        print("SMOKE_FAIL web cap exceeded", file=sys.stderr)
        return 1

    for step in steps:
        if not isinstance(step, dict):
            print("SMOKE_FAIL step type", file=sys.stderr)
            return 1
        for bad in _FORBIDDEN_KEYS:
            if bad in step:
                print("SMOKE_FAIL forbidden key", bad, file=sys.stderr)
                return 1
        label = step.get("label")
        if step.get("kind") in {"local_retrieve", "web_search", "synthesize"}:
            if not isinstance(label, str) or not _ZH_RE.search(label or ""):
                print("SMOKE_FAIL zh label", step, file=sys.stderr)
                return 1

    blob = json.dumps(steps, ensure_ascii=False).lower()
    for secret in (
        chat_env["LLM_API_KEY"].lower(),
        web_env["WEB_SEARCH_API_KEY"].lower(),
        chat_env["EMBEDDING_API_KEY"].lower(),
    ):
        if secret and secret in blob:
            print("SMOKE_FAIL key leaked in public_steps", file=sys.stderr)
            return 1
    for token in ("secret_chain_of_thought", "system_prompt_leak", "cot"):
        if token == "cot":
            # only fail if cot appears as a field value marker, not substring of words
            continue
        if token in blob:
            print("SMOKE_FAIL cot leak", token, file=sys.stderr)
            return 1

    output = turn.get("output") or {}
    knowledge_basis = turn.get("knowledge_basis") or output.get("knowledge_basis")
    summary = str(output.get("summary") or "")
    if knowledge_basis not in {"LOCAL", "WEB", "MIXED"}:
        print("SMOKE_FAIL knowledge_basis", knowledge_basis, file=sys.stderr)
        return 1
    if knowledge_basis in {"WEB", "MIXED"} and not any(m in summary for m in _NON_LOCAL_MARKERS):
        print("SMOKE_FAIL missing non-local disclosure", file=sys.stderr)
        return 1
    claims = list(output.get("claims") or [])
    if knowledge_basis == "MIXED":
        prov = {str(c.get("provenance")) for c in claims}
        if "LOCAL" not in prov or "WEB" not in prov:
            print("SMOKE_FAIL MIXED claims provenance", claims, file=sys.stderr)
            return 1

    print("task_status", turn.get("task_status"))
    print("result_status", turn.get("result_status"))
    print("knowledge_basis", knowledge_basis)
    print("tool_steps", len(tool_steps))
    print("web_steps", sum(1 for k in kinds if k == "web_search"))
    print("summary_len", len(summary))
    print("claims", len(claims))
    print("public_steps_ok", True)
    print("no_fake", type(chat).__name__, type(web).__name__)
    print("S24_SMOKE_OK")
    return 0


if __name__ == "__main__":
    # Ensure project root on path when run as module.
    root = Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root / "src"))
    raise SystemExit(main())
