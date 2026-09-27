"""S23 WebSearchPort RED contracts (REQ-015 / AC-019).

Focus (SPEC §1 WebSearchPort; §4 public web_search steps; §5 tool allowlist; §6 WEB_SEARCH_*):
- Fake adapter returns deterministic title/url/snippet hits (unit only)
- Public step payload exposes query + source title/URL; never api_key / CoT / raw HTML dump
- Http adapter construction keeps secrets out of repr/str
- max_results truncates; HTTP/timeout failures are explicit errors (not empty success)
- Fake must not be treatable as production evidence helper (type name / marker)
"""

from __future__ import annotations

import json
from typing import Any

import pytest


def _web_search_mod():
    try:
        import support_platform.infrastructure.web_search as mod

        return mod
    except ImportError as exc:
        pytest.fail(f"S23 web_search package missing: {exc}")


def _require_attr(mod: Any, name: str) -> Any:
    assert hasattr(mod, name), f"support_platform.infrastructure.web_search missing {name}"
    return getattr(mod, name)


def test_web_search_exports_port_fake_and_tavily() -> None:
    mod = _web_search_mod()
    for name in (
        "WebSearchPort",
        "WebSearchHit",
        "FakeWebSearchAdapter",
        "TavilyHttpWebSearchAdapter",
        "build_public_web_search_step",
    ):
        _require_attr(mod, name)


def test_fake_web_search_returns_title_url_snippet() -> None:
    mod = _web_search_mod()
    Fake = _require_attr(mod, "FakeWebSearchAdapter")
    fake = Fake(
        hits=[
            {
                "title": "Tavily Docs",
                "url": "https://example.com/tavily",
                "snippet": "paid web search API",
            }
        ]
    )
    result = fake.search(query="what is tavily", max_results=5)
    assert getattr(result, "query", None) == "what is tavily" or (
        isinstance(result, dict) and result.get("query") == "what is tavily"
    )
    hits = getattr(result, "hits", None) if not isinstance(result, dict) else result.get("hits")
    assert hits is not None and len(hits) >= 1
    first = hits[0]
    title = getattr(first, "title", None) if not isinstance(first, dict) else first.get("title")
    url = getattr(first, "url", None) if not isinstance(first, dict) else first.get("url")
    snippet = (
        getattr(first, "snippet", None) if not isinstance(first, dict) else first.get("snippet")
    )
    assert title == "Tavily Docs"
    assert url == "https://example.com/tavily"
    assert "paid web search" in (snippet or "")


def test_fake_respects_max_results() -> None:
    mod = _web_search_mod()
    Fake = _require_attr(mod, "FakeWebSearchAdapter")
    fake = Fake(
        hits=[
            {"title": f"H{i}", "url": f"https://example.com/{i}", "snippet": f"s{i}"}
            for i in range(10)
        ]
    )
    result = fake.search(query="q", max_results=3)
    hits = getattr(result, "hits", None) if not isinstance(result, dict) else result.get("hits")
    assert hits is not None
    assert len(hits) == 3


def test_public_web_search_step_exposes_query_title_url_without_secrets() -> None:
    mod = _web_search_mod()
    build = _require_attr(mod, "build_public_web_search_step")
    Fake = _require_attr(mod, "FakeWebSearchAdapter")
    fake = Fake(
        hits=[
            {
                "title": "Open Source License FAQ",
                "url": "https://example.com/license",
                "snippet": "MIT vs Apache",
            }
        ]
    )
    result = fake.search(query="MIT license difference", max_results=5)
    step = build(result, api_key="tvly-secret-should-never-appear")
    assert isinstance(step, dict)
    assert step.get("kind") == "web_search"
    assert step.get("label") == "网络检索"
    payload = {k: v for k, v in step.items() if k not in ("kind", "label")}
    blob = json.dumps(step, ensure_ascii=False)
    assert "MIT license difference" in blob or step.get("query") == "MIT license difference"
    assert "Open Source License FAQ" in blob
    assert "https://example.com/license" in blob
    assert "tvly-secret-should-never-appear" not in blob
    assert "api_key" not in blob.lower() or '"api_key"' not in blob
    # No raw HTML dump / CoT markers
    assert "<html" not in blob.lower()
    assert "chain of thought" not in blob.lower()
    assert "prompt" not in payload


def test_tavily_adapter_repr_and_str_redact_api_key() -> None:
    mod = _web_search_mod()
    Tavily = _require_attr(mod, "TavilyHttpWebSearchAdapter")
    adapter = Tavily(
        base_url="https://api.tavily.com",
        api_key="tvly-super-secret-key-value",
        max_results=5,
    )
    text = f"{adapter!r} {adapter!s}"
    assert "tvly-super-secret-key-value" not in text
    assert "api_key" not in text or "***" in text or "REDACTED" in text.upper() or "****" in text


def test_tavily_http_error_is_not_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Network/HTTP failure must raise — must not return zero hits as ANSWERED-like success."""
    mod = _web_search_mod()
    Tavily = _require_attr(mod, "TavilyHttpWebSearchAdapter")
    adapter = Tavily(
        base_url="https://api.tavily.com",
        api_key="tvly-test-key",
        max_results=5,
        timeout_s=1.0,
    )

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("simulated network down")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    with pytest.raises((OSError, RuntimeError, TimeoutError, ConnectionError)):
        adapter.search(query="should fail closed", max_results=3)


def test_ports_package_reexports_web_search_port() -> None:
    """Optional ports surface — Protocol must be importable for domain injection."""
    try:
        from support_platform.ports.web_search import WebSearchPort
    except ImportError as exc:
        pytest.fail(f"S23 ports.web_search.WebSearchPort missing: {exc}")
    assert WebSearchPort is not None


def test_fake_is_explicitly_marked_non_production() -> None:
    mod = _web_search_mod()
    Fake = _require_attr(mod, "FakeWebSearchAdapter")
    assert Fake.__name__ == "FakeWebSearchAdapter"
    fake = Fake(hits=[])
    assert "Fake" in type(fake).__name__
