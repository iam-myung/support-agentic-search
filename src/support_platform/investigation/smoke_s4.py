"""S4 real-path SMOKE: OP-00 investigate → service → real search + Chat."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from support_platform.infrastructure.chat import HttpChatAdapter
from support_platform.infrastructure.embedding import HttpEmbeddingAdapter
from support_platform.investigation import fail_stale_running, run_investigation
from support_platform.investigation.pg_store import PgInvestigationStore
from support_platform.knowledge.pg_import import persist_import_file, require_real_embedding_env
from support_platform.search.pg import retrieve_pg


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def require_real_chat_env() -> dict[str, str]:
    _load_dotenv()
    emb = require_real_embedding_env()
    required = ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"NOT_EXECUTED missing env: {', '.join(missing)}")
    if "fake" in os.environ["LLM_BASE_URL"].lower():
        raise RuntimeError("SMOKE_FAIL Fake LLM URL refused")
    return {
        **emb,
        "LLM_BASE_URL": os.environ["LLM_BASE_URL"],
        "LLM_API_KEY": os.environ["LLM_API_KEY"],
        "LLM_MODEL": os.environ["LLM_MODEL"],
    }


def _ensure_corpus(database_url: str) -> None:
    root = Path(__file__).resolve().parents[3]
    persist_import_file(
        path=root / "fixtures" / "corpus" / "guide.md",
        source_type="DOC",
        title="S4 Smoke Guide",
        database_url=database_url,
    )
    persist_import_file(
        path=root / "fixtures" / "corpus" / "faq_with_injection.md",
        source_type="FAQ",
        title="S4 Smoke FAQ",
        database_url=database_url,
    )
    print("corpus_import_ok")


def _make_retrieve(env: dict[str, str], embedding: HttpEmbeddingAdapter):
    def retrieve(query: str, **_: object):
        result = retrieve_pg(
            query=query,
            database_url=env["DATABASE_URL"],
            embedding=embedding,
            top_k=10,
            rrf_k=60,
        )
        return list(result.merged)

    return retrieve


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
    # Startup cleanup of leftover RUNNING (simulates process restart policy)
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
    if isinstance(chat, type) or type(chat).__name__ == "FakeChatAdapter":
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
    blob = json.dumps(steps, ensure_ascii=False).lower()
    if "retrieve" not in blob and "检索" not in blob:
        print("SMOKE_FAIL public steps missing retrieve", steps, file=sys.stderr)
        return 1
    for forbidden in ("api_key", "sk-", "system prompt", "chain of thought", "hidden_cot"):
        if forbidden in blob:
            print("SMOKE_FAIL leak", forbidden, file=sys.stderr)
            return 1
    print(
        "investigate_ok",
        result["id"],
        result["task_status"],
        "steps",
        len(steps),
        "kinds",
        [s.get("kind") for s in steps],
    )

    # Official CLI entry uses same service helpers (smoke owns real wiring)
    print("official_entry", "python -m support_platform.investigation.smoke_s4")
    print("also_op00", "python -m support_platform.cli investigate --question ...")

    # Kill-process leftover: insert RUNNING then fail_stale_running
    orphan = store.create_running(question="orphan after crash")
    assert store.get(orphan)["task_status"] == "RUNNING"
    changed = fail_stale_running(store)
    if orphan not in changed:
        print("SMOKE_FAIL orphan not cleaned", changed, file=sys.stderr)
        return 1
    row = store.get(orphan)
    if row["task_status"] != "FAILED" or row["error_code"] != "PROCESS_INTERRUPTED":
        print("SMOKE_FAIL orphan status", row, file=sys.stderr)
        return 1
    if row["result_status"] is not None:
        print("SMOKE_FAIL orphan result_status", row["result_status"], file=sys.stderr)
        return 1
    print("orphan_failed_ok", orphan, "PROCESS_INTERRUPTED")

    print("no_fake", "HttpChatAdapter+HttpEmbeddingAdapter+PostgreSQL")
    print("S4_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
