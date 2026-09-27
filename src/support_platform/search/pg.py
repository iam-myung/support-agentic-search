"""PostgreSQL dual-path retrieval: real embeddings + pg_trgm lexical."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from support_platform.infrastructure.embedding import HttpEmbeddingAdapter
from support_platform.search import SearchHit, SearchResult, _cosine, rrf_fuse

TOP_K_DEFAULT = 10
RRF_K_DEFAULT = 60


def _engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
        pool_timeout=5,
    )


def _load_active_chunks(engine: Engine) -> list[dict[str, Any]]:
    sql = text(
        """
        SELECT kc.id::text AS chunk_id,
               ks.title AS title,
               kc.content AS content,
               kc.lexical_text AS lexical_text,
               kc.embedding AS embedding,
               ks.status AS source_status,
               kv.status AS version_status,
               kv.is_active AS is_active
        FROM knowledge_chunks kc
        JOIN knowledge_versions kv ON kv.id = kc.version_id
        JOIN knowledge_sources ks ON ks.id = kv.source_id
        WHERE ks.status = 'ACTIVE'
          AND kv.status = 'READY'
          AND kv.is_active = true
          AND kc.embedding IS NOT NULL
        """
    )
    rows: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for row in conn.execute(sql):
            emb = row.embedding
            if isinstance(emb, str):
                emb = json.loads(emb)
            rows.append(
                {
                    "chunk_id": row.chunk_id,
                    "title": row.title,
                    "content": row.content,
                    "lexical_text": row.lexical_text or "",
                    "embedding": list(emb),
                    "source_status": row.source_status,
                    "version_status": row.version_status,
                    "is_active": bool(row.is_active),
                }
            )
    return rows


def _lexical_pg(
    engine: Engine,
    *,
    query: str,
    top_k: int,
) -> list[dict[str, Any]]:
    """Rank via pg_trgm similarity + exact substring/error-code preference."""
    sql = text(
        """
        SELECT kc.id::text AS chunk_id,
               similarity(coalesce(kc.lexical_text, ''), :q) AS sim,
               (position(upper(:q) in upper(coalesce(kc.content, ''))) > 0
                 OR position(upper(:q) in upper(coalesce(kc.lexical_text, ''))) > 0
                 OR position(upper(:q) in upper(ks.title)) > 0) AS exact_hit
        FROM knowledge_chunks kc
        JOIN knowledge_versions kv ON kv.id = kc.version_id
        JOIN knowledge_sources ks ON ks.id = kv.source_id
        WHERE ks.status = 'ACTIVE'
          AND kv.status = 'READY'
          AND kv.is_active = true
          AND (
            similarity(coalesce(kc.lexical_text, ''), :q) > 0.05
            OR position(upper(:q) in upper(coalesce(kc.content, ''))) > 0
            OR position(upper(:q) in upper(coalesce(kc.lexical_text, ''))) > 0
            OR position(upper(:q) in upper(ks.title)) > 0
          )
        ORDER BY exact_hit DESC, sim DESC, kc.id
        LIMIT :k
        """
    )
    out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for idx, row in enumerate(conn.execute(sql, {"q": query, "k": top_k}), start=1):
            out.append(
                {
                    "chunk_id": row.chunk_id,
                    "rank": idx,
                    "exact_error_code_hit": bool(row.exact_hit),
                }
            )
    return out


def _semantic_from_rows(
    *,
    query_vec: list[float],
    rows: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    scored = [
        (_cosine(query_vec, row["embedding"]), row) for row in rows if row.get("embedding")
    ]
    scored = [(s, r) for s, r in scored if s > 0.0]
    scored.sort(key=lambda item: (-item[0], item[1]["chunk_id"]))
    out: list[dict[str, Any]] = []
    for idx, (_score, row) in enumerate(scored[:top_k], start=1):
        out.append(
            {
                "chunk_id": row["chunk_id"],
                "rank": idx,
                "exact_error_code_hit": False,
            }
        )
    return out


def retrieve_pg(
    *,
    query: str,
    database_url: str,
    embedding: HttpEmbeddingAdapter,
    top_k: int = TOP_K_DEFAULT,
    rrf_k: int = RRF_K_DEFAULT,
) -> SearchResult:
    """Official real-path retrieve: PG active corpus + real query embedding + pg_trgm."""
    if not isinstance(embedding, HttpEmbeddingAdapter):
        raise RuntimeError("SMOKE/real path requires HttpEmbeddingAdapter (no Fake)")
    engine = _engine(database_url)
    rows = _load_active_chunks(engine)
    if not rows:
        raise RuntimeError("no ACTIVE/READY embedded chunks in PostgreSQL")
    query_vec = list(embedding.embed([query])[0])
    semantic = _semantic_from_rows(query_vec=query_vec, rows=rows, top_k=top_k)
    lexical = _lexical_pg(engine, query=query, top_k=top_k)
    fused = rrf_fuse(semantic, lexical, rrf_k=rrf_k)
    by_id = {r["chunk_id"]: r for r in rows}
    merged: list[SearchHit] = []
    for hit in fused:
        row = by_id[hit.chunk_id]
        merged.append(
            SearchHit(
                chunk_id=row["chunk_id"],
                channels=hit.channels,
                channel_ranks=dict(hit.channel_ranks),
                rrf_score=hit.rrf_score,
                exact_error_code_hit=hit.exact_error_code_hit,
                source_status=row["source_status"],
                version_status=row["version_status"],
                is_active=row["is_active"],
                content=row["content"],
                title=row["title"],
            )
        )
    return SearchResult(merged=merged)


def search_config_manifest(
    *,
    embedding_model: str,
    embedding_dimension: int,
    top_k: int = TOP_K_DEFAULT,
    rrf_k: int = RRF_K_DEFAULT,
) -> dict[str, Any]:
    return {
        "semantic_top_k": top_k,
        "lexical_top_k": top_k,
        "rrf_k": rrf_k,
        "tie_break": ["rrf_score", "exact_error_code_hit", "chunk_id"],
        "lexical_backend": "pg_trgm+exact_substring",
        "semantic_backend": "http_embedding+cosine_on_stored_json_vectors",
        "embedding_model": embedding_model,
        "embedding_dimension": embedding_dimension,
        "filter": "ACTIVE source + READY is_active version",
    }
