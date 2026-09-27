"""Chunk indexing with embedding model/dimension validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class EmbeddingPort(Protocol):
    model: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class IndexError_(ValueError):
    """Raised when embedding model/dimension does not match the locked corpus config."""


@dataclass
class IndexedBatch:
    embedding_model: str
    embedding_dimension: int
    vectors: list[list[float]]


def index_chunks(
    chunks: list[dict[str, Any]],
    *,
    embedding: EmbeddingPort,
    expected_model: str,
    expected_dimension: int,
) -> IndexedBatch:
    if embedding.model != expected_model:
        raise IndexError_(
            f"embedding model mismatch: got {embedding.model!r}, expected {expected_model!r}"
        )
    if embedding.dimension != expected_dimension:
        raise IndexError_(
            f"embedding dimension mismatch: got {embedding.dimension}, expected {expected_dimension}"
        )
    texts = [c["content"] for c in chunks]
    vectors = embedding.embed(texts)
    for vector in vectors:
        if len(vector) != expected_dimension:
            raise IndexError_(
                f"embedding dimension mismatch in vector length={len(vector)}, expected {expected_dimension}"
            )
    return IndexedBatch(
        embedding_model=embedding.model,
        embedding_dimension=embedding.dimension,
        vectors=vectors,
    )
