"""S3 real-path SMOKE: fixed queries against PostgreSQL + real Embedding."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from support_platform.infrastructure.embedding import HttpEmbeddingAdapter
from support_platform.knowledge.pg_import import persist_import_file, require_real_embedding_env
from support_platform.search.pg import retrieve_pg, search_config_manifest

TOP_K = 10
RRF_K = 60


def _ensure_fixed_corpus(database_url: str) -> None:
    """Import synthetic fixtures so fixed queries have ACTIVE/READY evidence."""
    root = Path(__file__).resolve().parents[3]
    guide = root / "fixtures" / "corpus" / "guide.md"
    faq = root / "fixtures" / "corpus" / "faq_with_injection.md"
    persist_import_file(
        path=guide,
        source_type="DOC",
        title="S3 Smoke Guide",
        database_url=database_url,
    )
    persist_import_file(
        path=faq,
        source_type="FAQ",
        title="S3 Smoke FAQ",
        database_url=database_url,
    )
    print("corpus_import_ok", guide.name, faq.name)


def main() -> int:
    try:
        env = require_real_embedding_env()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if "fake" in env["EMBEDDING_BASE_URL"].lower():
        print("SMOKE_FAIL Fake embedding URL refused", file=sys.stderr)
        return 1

    dimension = int(env["EMBEDDING_DIMENSION"])
    adapter = HttpEmbeddingAdapter(
        base_url=env["EMBEDDING_BASE_URL"],
        api_key=env["EMBEDDING_API_KEY"],
        model=env["EMBEDDING_MODEL"],
        dimension=dimension,
    )

    print("embedding_provider", "dashscope-http")
    print("embedding_model", env["EMBEDDING_MODEL"])
    print("embedding_dimension", dimension)
    print("database", env["DATABASE_URL"].split("@")[-1])
    print("top_k", TOP_K, "rrf_k", RRF_K)

    try:
        _ensure_fixed_corpus(env["DATABASE_URL"])
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL corpus", exc, file=sys.stderr)
        return 1

    manifest = search_config_manifest(
        embedding_model=env["EMBEDDING_MODEL"],
        embedding_dimension=dimension,
        top_k=TOP_K,
        rrf_k=RRF_K,
    )
    print("search_config_manifest", json.dumps(manifest, ensure_ascii=False))

    # Fixed query A: semantic paraphrase → password / settings chunk
    q_semantic = "how do I change my password"
    sem = retrieve_pg(
        query=q_semantic,
        database_url=env["DATABASE_URL"],
        embedding=adapter,
        top_k=TOP_K,
        rrf_k=RRF_K,
    )
    if not sem.merged:
        print("SMOKE_FAIL semantic empty", file=sys.stderr)
        return 1
    password_hit = next(
        (
            h
            for h in sem.merged
            if "password" in h.content.lower() or "reset" in h.content.lower()
        ),
        None,
    )
    if password_hit is None:
        print("SMOKE_FAIL semantic miss password", file=sys.stderr)
        return 1
    if "semantic" not in password_hit.channels:
        print("SMOKE_FAIL semantic channel missing", password_hit.channels, file=sys.stderr)
        return 1
    print(
        "semantic_ok",
        password_hit.chunk_id,
        "channels",
        password_hit.channels,
        "ranks",
        password_hit.channel_ranks,
    )

    # Fixed query B: exact error code via lexical / pg_trgm path
    q_lex = "E1001"
    lex = retrieve_pg(
        query=q_lex,
        database_url=env["DATABASE_URL"],
        embedding=adapter,
        top_k=TOP_K,
        rrf_k=RRF_K,
    )
    if not lex.merged:
        print("SMOKE_FAIL lexical empty", file=sys.stderr)
        return 1
    hit = next((h for h in lex.merged if "E1001" in h.content), None)
    if hit is None:
        print("SMOKE_FAIL lexical miss E1001", file=sys.stderr)
        return 1
    if "lexical" not in hit.channels:
        print("SMOKE_FAIL lexical channel missing", hit.channels, file=sys.stderr)
        return 1
    if not isinstance(hit.channel_ranks.get("lexical"), int):
        print("SMOKE_FAIL lexical rank missing", file=sys.stderr)
        return 1
    print(
        "lexical_ok",
        hit.chunk_id,
        "channels",
        hit.channels,
        "ranks",
        hit.channel_ranks,
        "exact",
        hit.exact_error_code_hit,
    )

    # Reproducibility: same query twice → same ordered chunk ids
    again = retrieve_pg(
        query=q_lex,
        database_url=env["DATABASE_URL"],
        embedding=adapter,
        top_k=TOP_K,
        rrf_k=RRF_K,
    )
    ids1 = [h.chunk_id for h in lex.merged]
    ids2 = [h.chunk_id for h in again.merged]
    if ids1 != ids2:
        print("SMOKE_FAIL unstable ranking", ids1, ids2, file=sys.stderr)
        return 1
    print("rrf_stable_ok", ids1[:5])

    print("official_entry", "python -m support_platform.search.smoke_s3")
    print("no_fake", "HttpEmbeddingAdapter+PostgreSQL+pg_trgm")
    print("S3_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
