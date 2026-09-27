"""S8 real-path SMOKE: OP-06 evaluate ≥30 cases → real Chat + Embedding + PG.

Official entry:
  python -m support_platform.evaluation.smoke_s8
  (or) python -m support_platform.cli evaluate --dataset fixtures/eval/phase1_30.json --real
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from support_platform.evaluation import compare_to_baseline, dataset_hash, run_evaluation
from support_platform.infrastructure.chat import HttpChatAdapter
from support_platform.infrastructure.embedding import HttpEmbeddingAdapter
from support_platform.investigation import fail_stale_running, run_investigation
from support_platform.investigation.pg_store import PgInvestigationStore
from support_platform.investigation.smoke_s4 import (
    _make_retrieve,
    require_real_chat_env,
)
from support_platform.knowledge.pg_import import persist_import_file

REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET_PATH = REPO_ROOT / "fixtures" / "eval" / "phase1_30.json"
OUTPUT_DIR = REPO_ROOT / "artifacts" / "evaluation"
PROMPT_VERSION = "phase1-investigation-v1"
RETRIEVAL_CONFIG_VERSION = "rrf-k60-topk10-v1"


class _BaselineChat:
    """Wrap real chat so baseline runs never trigger a second retrieval wave."""

    def __init__(self, inner: HttpChatAdapter) -> None:
        self._inner = inner

    def assess(self, *, question: str, hits: list[Any], public_only: bool = True) -> dict[str, Any]:
        raw = self._inner.assess(question=question, hits=hits, public_only=public_only)
        # Force single-pass for baseline comparison (PRD §4.1).
        return {
            **raw,
            "next_action": "sufficient",
            "gaps": [],
        }

    def rewrite(self, *, question: str, constraints: dict[str, Any], gaps: list[str]) -> str:
        return self._inner.rewrite(question=question, constraints=constraints, gaps=gaps)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ensure_eval_corpus(database_url: str) -> list[str]:
    """Import frozen eval corpus; return list of content hashes / version ids for provenance."""
    corpus = [
        ("DOC", "S8 Eval Guide", REPO_ROOT / "fixtures" / "corpus" / "guide.md"),
        ("FAQ", "S8 Eval FAQ", REPO_ROOT / "fixtures" / "corpus" / "eval_faq.md"),
        ("DOC", "S8 MFA Yes", REPO_ROOT / "fixtures" / "corpus" / "eval_mfa_yes.md"),
        ("DOC", "S8 MFA No", REPO_ROOT / "fixtures" / "corpus" / "eval_mfa_no.md"),
        ("DOC", "S8 Refund Risk", REPO_ROOT / "fixtures" / "corpus" / "eval_refund_risk.md"),
        ("FAQ", "S8 Injection FAQ", REPO_ROOT / "fixtures" / "corpus" / "faq_with_injection.md"),
    ]
    version_ids: list[str] = []
    for source_type, title, path in corpus:
        result = persist_import_file(
            path=path,
            source_type=source_type,
            title=title,
            database_url=database_url,
        )
        version_ids.append(str(result["version_id"]))
        print("imported", title, "chunks", result["chunk_count"], "sha", _file_sha256(path)[:12])
    return version_ids


def _load_cases(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "cases" in raw:
        cases = list(raw["cases"])
    elif isinstance(raw, list):
        cases = list(raw)
    else:
        raise ValueError("dataset must be a list or {cases: [...]}")
    if len(cases) < 30:
        raise ValueError(f"SMOKE requires >=30 cases, got {len(cases)}")
    return cases


def _make_investigate(
    *,
    env: dict[str, str],
    embedding: HttpEmbeddingAdapter,
    chat: Any,
    store: PgInvestigationStore,
):
    retrieve = _make_retrieve(env, embedding)

    def investigate(
        *,
        question: str,
        context: dict[str, Any] | None = None,
        baseline: bool = False,
        case_id: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        _ = context, case_id
        adapter = _BaselineChat(chat) if baseline else chat
        return run_investigation(
            question=question,
            retrieve=retrieve,
            chat=adapter,
            store=store,
        )

    return investigate


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
    print("user_approval", "S8-SMOKE paid Chat+Embedding")
    print("official_entry", "python -m support_platform.evaluation.smoke_s8")
    print("dataset_path", DATASET_PATH)

    if type(HttpChatAdapter).__name__ == "FakeChatAdapter":
        print("SMOKE_FAIL Fake chat class", file=sys.stderr)
        return 1

    try:
        cases = _load_cases(DATASET_PATH)
        version_ids = _ensure_eval_corpus(env["DATABASE_URL"])
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL prepare", exc, file=sys.stderr)
        return 1

    knowledge_snapshot_hash = hashlib.sha256(
        ("|".join(sorted(version_ids))).encode("utf-8")
    ).hexdigest()
    model_config_version = f"llm:{env['LLM_MODEL']}|emb:{env['EMBEDDING_MODEL']}"
    ds_hash = dataset_hash(cases)
    print("dataset_hash", ds_hash)
    print("knowledge_snapshot_hash", knowledge_snapshot_hash)
    print("case_count", len(cases))

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

    investigate = _make_investigate(env=env, embedding=embedding, chat=chat, store=store)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        primary = run_evaluation(
            dataset=cases,
            investigate=investigate,
            baseline=False,
            knowledge_snapshot_hash=knowledge_snapshot_hash,
            prompt_version=PROMPT_VERSION,
            model_config_version=model_config_version,
            retrieval_config_version=RETRIEVAL_CONFIG_VERSION,
            output_dir=OUTPUT_DIR,
        )
        baseline = run_evaluation(
            dataset=cases,
            investigate=investigate,
            baseline=True,
            knowledge_snapshot_hash=knowledge_snapshot_hash,
            prompt_version=PROMPT_VERSION,
            model_config_version=model_config_version,
            retrieval_config_version=RETRIEVAL_CONFIG_VERSION,
            output_dir=OUTPUT_DIR,
        )
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL evaluate", exc, file=sys.stderr)
        return 1

    comparison = compare_to_baseline(primary=primary, baseline=baseline)
    summary = {
        "primary_metrics": primary.get("metrics"),
        "baseline_metrics": baseline.get("metrics"),
        "primary_failures": len(primary.get("failures") or []),
        "baseline_failures": len(baseline.get("failures") or []),
        "comparison": comparison,
        "primary_output": primary.get("output_path"),
        "baseline_output": baseline.get("output_path"),
        "manifest_primary": primary.get("manifest"),
    }
    summary_path = OUTPUT_DIR / f"s8_smoke_summary_{ds_hash[:12]}.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print("summary_path", summary_path)
    print("primary_status_accuracy", (primary.get("metrics") or {}).get("status_accuracy"))
    print("baseline_status_accuracy", (baseline.get("metrics") or {}).get("status_accuracy"))
    print("primary_failure_count", len(primary.get("failures") or []))
    print("comparable", comparison.get("comparable"))
    if comparison.get("comparable"):
        print("relative_improvement", comparison.get("relative_improvement"))
    else:
        print("relative_improvement", "N/A", comparison.get("reason"))

    # Failures must remain visible — do not claim production or 3% gate.
    print("note", "Phase1 prototype eval only; not production; not AC-014 3% claim")
    cleaned_end = fail_stale_running(store)
    print("stale_cleanup_end", len(cleaned_end))
    print("S8_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
