"""S20 real-path SMOKE: OP-00 investigate → Phase3 labels + non-empty summary.

Requires real Chat + Embedding + PostgreSQL. Fake adapters are refused.
Paid provider calls require explicit user approval for this Step.
"""

from __future__ import annotations

import json
import re
import sys

from support_platform.infrastructure.chat import HttpChatAdapter
from support_platform.infrastructure.embedding import HttpEmbeddingAdapter
from support_platform.investigation import fail_stale_running, run_investigation
from support_platform.investigation.pg_store import PgInvestigationStore
from support_platform.investigation.smoke_s4 import (
    _ensure_corpus,
    _make_retrieve,
    require_real_chat_env,
)

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")
_REQUIRED_KIND_TOKENS = ("understand", "plan", "retrieve", "compare", "synthesize")
_FORBIDDEN_KEYS = frozenset({"prompt", "system", "cot", "api_key", "internal"})
_FORBIDDEN_BLOB = (
    "api_key",
    "sk-",
    "system prompt",
    "chain of thought",
    "hidden_cot",
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
    question = "How do I reset my password? Also what does error E1001 mean?"
    try:
        result = run_investigation(
            question=question,
            retrieve=retrieve,
            chat=chat,
            store=store,
        )
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL investigate", exc, file=sys.stderr)
        return 1

    if result["task_status"] not in {"COMPLETED", "FAILED"}:
        print("SMOKE_FAIL bad status", result["task_status"], file=sys.stderr)
        return 1

    steps = result.get("public_steps") or []
    if not isinstance(steps, list) or len(steps) < 3:
        print("SMOKE_FAIL public_steps too short", steps, file=sys.stderr)
        return 1

    kinds_blob = " ".join(str(s.get("kind") or "") for s in steps).lower()
    for token in _REQUIRED_KIND_TOKENS:
        if token not in kinds_blob:
            print("SMOKE_FAIL missing kind token", token, kinds_blob, file=sys.stderr)
            return 1

    for step in steps:
        if not isinstance(step, dict):
            print("SMOKE_FAIL step not dict", step, file=sys.stderr)
            return 1
        label = step.get("label")
        if not isinstance(label, str) or not label.strip():
            print("SMOKE_FAIL missing zh label", step, file=sys.stderr)
            return 1
        if not _ZH_RE.search(label):
            print("SMOKE_FAIL label not Chinese-visible", label, file=sys.stderr)
            return 1
        leak_keys = _FORBIDDEN_KEYS.intersection(step.keys())
        if leak_keys:
            print("SMOKE_FAIL forbidden keys", sorted(leak_keys), file=sys.stderr)
            return 1

    blob = json.dumps(steps, ensure_ascii=False).lower()
    for forbidden in _FORBIDDEN_BLOB:
        if forbidden in blob:
            print("SMOKE_FAIL leak", forbidden, file=sys.stderr)
            return 1

    output = result.get("output") or {}
    if not isinstance(output, dict):
        print("SMOKE_FAIL output not dict", output, file=sys.stderr)
        return 1
    result_status = result.get("result_status")
    summary = output.get("summary")
    if result_status in {"ANSWERED", "CONFLICT"}:
        if not isinstance(summary, str) or not summary.strip():
            print(
                "SMOKE_FAIL nonempty summary required for",
                result_status,
                output,
                file=sys.stderr,
            )
            return 1
    elif isinstance(summary, str) and summary.strip():
        print("summary_present_optional", result_status, len(summary.strip()))
    else:
        print("summary_absent_ok_for", result_status)

    print(
        "investigate_ok",
        result["id"],
        result["task_status"],
        result_status,
        "steps",
        len(steps),
        "kinds",
        [s.get("kind") for s in steps],
        "labels",
        [s.get("label") for s in steps],
        "summary_len",
        len((summary or "").strip()) if isinstance(summary, str) else 0,
    )
    print("official_entry", "python -m support_platform.investigation.smoke_s20")
    print("also_op00", "python -m support_platform.cli investigate --question ...")
    print("no_fake", "HttpChatAdapter+HttpEmbeddingAdapter+PostgreSQL")
    print("S20_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
