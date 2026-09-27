"""Embedding adapters: Fake (tests) and Real HTTP (SMOKE when approved)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class FakeEmbeddingAdapter:
    def __init__(self, *, model: str, dimension: int) -> None:
        self.model = model
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float((i + len(text)) % 7) for i in range(self.dimension)] for text in texts]


class HttpEmbeddingAdapter:
    """Minimal OpenAI-compatible embeddings client for approved SMOKE only."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int,
        timeout_s: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimension = dimension
        self.timeout_s = timeout_s

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload_obj: dict[str, Any] = {"model": self.model, "input": texts}
        # DashScope text-embedding-v3 supports explicit dimensions in compatible mode.
        if self.dimension > 0:
            payload_obj["dimensions"] = self.dimension
        payload = json.dumps(payload_obj).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:  # pragma: no cover
            raise RuntimeError(f"embedding request failed: {exc}") from exc
        data = sorted(body.get("data", []), key=lambda item: item.get("index", 0))
        vectors = [item["embedding"] for item in data]
        for vector in vectors:
            if len(vector) != self.dimension:
                raise RuntimeError(
                    f"embedding dimension mismatch: got {len(vector)}, expected {self.dimension}"
                )
        return vectors
