"""S3 dual-path retrieval + RRF RED contracts (REQ-003 / AC-003)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

TOP_K = 10
RRF_K = 60


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    content: str
    lexical_text: str
    embedding: list[float]
    title: str
    source_status: str = "ACTIVE"
    version_status: str = "READY"
    is_active: bool = True
    error_codes: tuple[str, ...] = ()


@dataclass
class FixedEmbedding:
    """Deterministic embedder: exact text → vector; unknown → zeros."""

    model: str
    dimension: int
    table: dict[str, list[float]] = field(default_factory=dict)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            if text in self.table:
                out.append(list(self.table[text]))
            else:
                out.append([0.0] * self.dimension)
        return out


def _corpus() -> list[ChunkRecord]:
    # password-reset topic vector ≈ query paraphrase vector
    reset_vec = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    other_vec = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    code_vec = [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    return [
        ChunkRecord(
            chunk_id="c-reset",
            content="Users can reset their account password from Settings.",
            lexical_text="reset account password settings",
            embedding=reset_vec,
            title="Account Guide",
        ),
        ChunkRecord(
            chunk_id="c-billing",
            content="Billing cycles renew on the first of each month.",
            lexical_text="billing renew month",
            embedding=other_vec,
            title="Billing FAQ",
        ),
        ChunkRecord(
            chunk_id="c-err",
            content="If the client shows ERR_AUTH_9021, refresh the token.",
            lexical_text="ERR_AUTH_9021 refresh token client error",
            embedding=code_vec,
            title="Auth Errors",
            error_codes=("ERR_AUTH_9021",),
        ),
        ChunkRecord(
            chunk_id="c-disabled",
            content="Obsolete password page still mentions reset.",
            lexical_text="obsolete reset password",
            embedding=reset_vec,
            title="Old Guide",
            source_status="DISABLED",
            is_active=False,
        ),
    ]


def test_semantic_paraphrase_hits_relevant_chunk() -> None:
    from support_platform.search import retrieve

    query = "how do I change my password"
    emb = FixedEmbedding(
        model="fake-emb",
        dimension=8,
        table={query: [0.95, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
    )
    result = retrieve(
        query=query,
        chunks=_corpus(),
        embedding=emb,
        top_k=TOP_K,
        rrf_k=RRF_K,
    )
    merged_ids = [hit.chunk_id for hit in result.merged]
    assert "c-reset" in merged_ids
    assert "c-disabled" not in merged_ids
    reset = next(hit for hit in result.merged if hit.chunk_id == "c-reset")
    assert "semantic" in reset.channels
    assert isinstance(reset.channel_ranks["semantic"], int)
    assert reset.channel_ranks["semantic"] >= 1


def test_exact_error_code_hits_via_lexical_channel() -> None:
    from support_platform.search import retrieve

    query = "ERR_AUTH_9021"
    emb = FixedEmbedding(model="fake-emb", dimension=8, table={query: [0.0] * 8})
    result = retrieve(
        query=query,
        chunks=_corpus(),
        embedding=emb,
        top_k=TOP_K,
        rrf_k=RRF_K,
    )
    merged_ids = [hit.chunk_id for hit in result.merged]
    assert "c-err" in merged_ids
    err = next(hit for hit in result.merged if hit.chunk_id == "c-err")
    assert "lexical" in err.channels
    assert isinstance(err.channel_ranks["lexical"], int)
    assert err.channel_ranks["lexical"] >= 1
    assert err.exact_error_code_hit is True


def test_rrf_preserves_channel_names_and_original_ranks() -> None:
    from support_platform.search import rrf_fuse

    semantic = [
        {"chunk_id": "a", "rank": 1},
        {"chunk_id": "b", "rank": 2},
    ]
    lexical = [
        {"chunk_id": "b", "rank": 1},
        {"chunk_id": "c", "rank": 2},
    ]
    fused = rrf_fuse(semantic, lexical, rrf_k=RRF_K)
    by_id = {item.chunk_id: item for item in fused}
    assert set(by_id) == {"a", "b", "c"}
    assert by_id["a"].channels == ("semantic",) or "semantic" in by_id["a"].channels
    assert by_id["a"].channel_ranks["semantic"] == 1
    assert "lexical" not in by_id["a"].channel_ranks
    assert by_id["b"].channel_ranks["semantic"] == 2
    assert by_id["b"].channel_ranks["lexical"] == 1
    # b appears in both → higher RRF than single-channel peers with worse ranks
    assert by_id["b"].rrf_score > by_id["a"].rrf_score
    assert by_id["b"].rrf_score > by_id["c"].rrf_score


def test_rrf_tie_break_is_stable_and_reproducible() -> None:
    from support_platform.search import rrf_fuse

    # Same RRF contribution (rank 1 only in one channel each) → tie on score
    semantic = [
        {"chunk_id": "z-chunk", "rank": 1, "exact_error_code_hit": False},
        {"chunk_id": "a-chunk", "rank": 1, "exact_error_code_hit": False},
    ]
    lexical: list[dict] = []
    first = [item.chunk_id for item in rrf_fuse(semantic, lexical, rrf_k=RRF_K)]
    second = [item.chunk_id for item in rrf_fuse(semantic, lexical, rrf_k=RRF_K)]
    assert len(first) == 2
    assert first == second
    # break order: RRF → exact_error_code_hit → chunk_id ascending
    assert first == ["a-chunk", "z-chunk"]


def test_exact_error_code_wins_rrf_tie_break() -> None:
    from support_platform.search import rrf_fuse

    semantic = [
        {"chunk_id": "plain", "rank": 1, "exact_error_code_hit": False},
        {"chunk_id": "coded", "rank": 1, "exact_error_code_hit": True},
    ]
    fused = rrf_fuse(semantic, [], rrf_k=RRF_K)
    assert len(fused) == 2
    assert fused[0].chunk_id == "coded"
    assert fused[0].exact_error_code_hit is True
    assert fused[1].chunk_id == "plain"


def test_disabled_or_inactive_chunks_never_retrieved() -> None:
    from support_platform.search import retrieve

    query = "how do I change my password"
    emb = FixedEmbedding(
        model="fake-emb",
        dimension=8,
        table={query: [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
    )
    result = retrieve(
        query=query,
        chunks=_corpus(),
        embedding=emb,
        top_k=TOP_K,
        rrf_k=RRF_K,
    )
    merged_ids = [hit.chunk_id for hit in result.merged]
    assert "c-reset" in merged_ids
    assert "c-disabled" not in merged_ids
    for hit in result.merged:
        assert hit.source_status == "ACTIVE"
        assert hit.version_status == "READY"
        assert hit.is_active is True
