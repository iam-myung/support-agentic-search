"""Dual-path retrieval + RRF fusion (REQ-003 / AC-003)."""

from __future__ import annotations

import math
from typing import Any


class SearchHit:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class SearchResult:
    def __init__(self, merged: list[Any] | None = None) -> None:
        self.merged = merged or []


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _eligible(chunks: list[Any]) -> list[Any]:
    return [
        c
        for c in chunks
        if getattr(c, "source_status", None) == "ACTIVE"
        and getattr(c, "version_status", None) == "READY"
        and getattr(c, "is_active", False) is True
    ]


def rrf_fuse(
    semantic: list[dict[str, Any]],
    lexical: list[dict[str, Any]],
    *,
    rrf_k: int = 60,
) -> list[SearchHit]:
    """Fuse channel rankings: score=Σ 1/(rrf_k+rank); break by exact code then chunk_id."""
    buckets: dict[str, dict[str, Any]] = {}

    def _ingest(channel: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            cid = str(row["chunk_id"])
            rank = int(row["rank"])
            exact = bool(row.get("exact_error_code_hit", False))
            slot = buckets.setdefault(
                cid,
                {
                    "chunk_id": cid,
                    "channel_ranks": {},
                    "rrf_score": 0.0,
                    "exact_error_code_hit": False,
                },
            )
            slot["channel_ranks"][channel] = rank
            slot["rrf_score"] += 1.0 / (rrf_k + rank)
            slot["exact_error_code_hit"] = slot["exact_error_code_hit"] or exact

    _ingest("semantic", semantic)
    _ingest("lexical", lexical)

    hits: list[SearchHit] = []
    for slot in buckets.values():
        ranks: dict[str, int] = dict(slot["channel_ranks"])
        channels = tuple(sorted(ranks.keys()))
        hits.append(
            SearchHit(
                chunk_id=slot["chunk_id"],
                channels=channels,
                channel_ranks=ranks,
                rrf_score=float(slot["rrf_score"]),
                exact_error_code_hit=bool(slot["exact_error_code_hit"]),
            )
        )

    hits.sort(
        key=lambda h: (
            -float(h.rrf_score),
            -int(bool(h.exact_error_code_hit)),
            str(h.chunk_id),
        )
    )
    return hits


def _semantic_hits(
    *,
    query_vec: list[float],
    chunks: list[Any],
    top_k: int,
) -> list[dict[str, Any]]:
    scored: list[tuple[float, Any]] = [
        (_cosine(query_vec, list(c.embedding)), c) for c in chunks
    ]
    # Drop non-positive similarity so zero query vectors do not invent ranks.
    scored = [(s, c) for s, c in scored if s > 0.0]
    scored.sort(key=lambda item: (-item[0], str(item[1].chunk_id)))
    out: list[dict[str, Any]] = []
    for idx, (_score, chunk) in enumerate(scored[:top_k], start=1):
        out.append(
            {
                "chunk_id": chunk.chunk_id,
                "rank": idx,
                "exact_error_code_hit": False,
            }
        )
    return out


def _lexical_hits(
    *,
    query: str,
    chunks: list[Any],
    top_k: int,
) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        return []
    scored: list[tuple[float, bool, Any]] = []
    for chunk in chunks:
        codes = tuple(getattr(chunk, "error_codes", ()) or ())
        lexical_text = str(getattr(chunk, "lexical_text", "") or "")
        title = str(getattr(chunk, "title", "") or "")
        content = str(getattr(chunk, "content", "") or "")
        exact_code = q in codes
        # Title / error-code exact match (SPEC §1)
        exact_title = q == title or q in title.split()
        in_lexical = q in lexical_text
        score = 0.0
        if exact_code:
            score += 1000.0
        if exact_title:
            score += 500.0
        if in_lexical:
            score += 100.0
        # Lightweight token overlap standing in for pg_trgm in unit path
        q_tokens = {t for t in q.lower().split() if t}
        l_tokens = {t for t in f"{lexical_text} {title}".lower().split() if t}
        score += float(len(q_tokens & l_tokens))
        if score <= 0.0 and q.lower() in content.lower():
            score += 1.0
        if score > 0.0:
            scored.append((score, exact_code or (in_lexical and q.startswith("ERR_")), chunk))

    scored.sort(key=lambda item: (-item[0], -int(item[1]), str(item[2].chunk_id)))
    out: list[dict[str, Any]] = []
    for idx, (_score, exact, chunk) in enumerate(scored[:top_k], start=1):
        codes = tuple(getattr(chunk, "error_codes", ()) or ())
        out.append(
            {
                "chunk_id": chunk.chunk_id,
                "rank": idx,
                "exact_error_code_hit": bool(exact or q in codes),
            }
        )
    return out


def retrieve(
    *,
    query: str,
    chunks: list[Any],
    embedding: Any,
    top_k: int = 10,
    rrf_k: int = 60,
) -> SearchResult:
    eligible = _eligible(chunks)
    query_vec = list(embedding.embed([query])[0])
    semantic = _semantic_hits(query_vec=query_vec, chunks=eligible, top_k=top_k)
    lexical = _lexical_hits(query=query, chunks=eligible, top_k=top_k)
    fused = rrf_fuse(semantic, lexical, rrf_k=rrf_k)
    by_id = {c.chunk_id: c for c in eligible}
    merged: list[SearchHit] = []
    for hit in fused:
        chunk = by_id[hit.chunk_id]
        merged.append(
            SearchHit(
                chunk_id=chunk.chunk_id,
                channels=hit.channels,
                channel_ranks=dict(hit.channel_ranks),
                rrf_score=hit.rrf_score,
                exact_error_code_hit=hit.exact_error_code_hit,
                source_status=chunk.source_status,
                version_status=chunk.version_status,
                is_active=chunk.is_active,
                content=chunk.content,
                title=chunk.title,
            )
        )
    return SearchResult(merged=merged)
