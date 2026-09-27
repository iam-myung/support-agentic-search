"""Web search port: Fake (tests) and Tavily HTTP (SMOKE when approved)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class WebSearchHit:
    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True)
class WebSearchResult:
    query: str
    hits: list[WebSearchHit] = field(default_factory=list)


@runtime_checkable
class WebSearchPort(Protocol):
    def search(self, *, query: str, max_results: int = 5) -> WebSearchResult:
        """Run a web search; never log or return the API key."""


def _as_hit(item: WebSearchHit | dict[str, Any]) -> WebSearchHit:
    if isinstance(item, WebSearchHit):
        return item
    return WebSearchHit(
        title=str(item.get("title") or ""),
        url=str(item.get("url") or ""),
        snippet=str(item.get("snippet") or ""),
    )


class FakeWebSearchAdapter:
    """Deterministic search results for unit tests only — never SMOKE evidence."""

    def __init__(self, *, hits: list[WebSearchHit | dict[str, Any]] | None = None) -> None:
        self._hits = [_as_hit(h) for h in (hits or [])]

    def search(self, *, query: str, max_results: int = 5) -> WebSearchResult:
        limit = max(0, int(max_results))
        return WebSearchResult(query=query, hits=list(self._hits[:limit]))


class TavilyHttpWebSearchAdapter:
    """Minimal Tavily HTTP client for approved paid SMOKE only."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        max_results: int = 5,
        timeout_s: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.max_results = max_results
        self.timeout_s = timeout_s

    def __repr__(self) -> str:
        return (
            f"TavilyHttpWebSearchAdapter(base_url={self.base_url!r}, "
            f"api_key='***', max_results={self.max_results})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def search(self, *, query: str, max_results: int | None = None) -> WebSearchResult:
        limit = int(self.max_results if max_results is None else max_results)
        payload = json.dumps(
            {
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/search",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"web_search HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"web_search request failed: {exc}") from exc

        raw_hits = body.get("results") or []
        hits: list[WebSearchHit] = []
        for item in raw_hits[:limit]:
            if not isinstance(item, dict):
                continue
            hits.append(
                WebSearchHit(
                    title=str(item.get("title") or ""),
                    url=str(item.get("url") or ""),
                    snippet=str(item.get("content") or item.get("snippet") or ""),
                )
            )
        return WebSearchResult(query=str(body.get("query") or query), hits=hits)


def build_public_web_search_step(
    result: WebSearchResult | dict[str, Any],
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Build a public process step — never include secrets, CoT, or raw HTML."""
    _ = api_key  # accepted for call-site convenience; intentionally discarded
    if isinstance(result, dict):
        query = str(result.get("query") or "")
        raw_hits = result.get("hits") or []
        hits = [_as_hit(h) for h in raw_hits]
    else:
        query = result.query
        hits = list(result.hits)

    sources = [{"title": h.title, "url": h.url} for h in hits if h.title or h.url]
    return {
        "kind": "web_search",
        "label": "网络检索",
        "query": query,
        "sources": sources,
    }
