"""S23 real-path SMOKE: TavilyHttpWebSearchAdapter → live paid search.

Requires WEB_SEARCH_* and explicit user approval for this Step.
Fake adapters are refused. Do not print API keys.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from support_platform.infrastructure.web_search import (
    FakeWebSearchAdapter,
    TavilyHttpWebSearchAdapter,
    build_public_web_search_step,
)


def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_real_web_search_env() -> dict[str, str]:
    _load_dotenv()
    required = (
        "WEB_SEARCH_PROVIDER",
        "WEB_SEARCH_BASE_URL",
        "WEB_SEARCH_API_KEY",
    )
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"missing env: {','.join(missing)}")
    provider = os.environ["WEB_SEARCH_PROVIDER"].strip().lower()
    if provider not in {"tavily", "http", "tavily-http"}:
        raise RuntimeError(f"unsupported WEB_SEARCH_PROVIDER={provider}")
    max_results = os.environ.get("WEB_SEARCH_MAX_RESULTS") or "5"
    return {
        "WEB_SEARCH_PROVIDER": provider,
        "WEB_SEARCH_BASE_URL": os.environ["WEB_SEARCH_BASE_URL"].rstrip("/"),
        "WEB_SEARCH_API_KEY": os.environ["WEB_SEARCH_API_KEY"],
        "WEB_SEARCH_MAX_RESULTS": max_results,
    }


def main() -> int:
    try:
        env = require_real_web_search_env()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print("web_search_provider", env["WEB_SEARCH_PROVIDER"])
    print("web_search_base", env["WEB_SEARCH_BASE_URL"])
    print("web_search_max_results", env["WEB_SEARCH_MAX_RESULTS"])
    print("web_search_api_key", "SET")

    adapter = TavilyHttpWebSearchAdapter(
        base_url=env["WEB_SEARCH_BASE_URL"],
        api_key=env["WEB_SEARCH_API_KEY"],
        max_results=int(env["WEB_SEARCH_MAX_RESULTS"]),
        timeout_s=45.0,
    )
    if type(adapter).__name__ == "FakeWebSearchAdapter" or isinstance(
        adapter, FakeWebSearchAdapter
    ):
        print("SMOKE_FAIL Fake web_search", file=sys.stderr)
        return 1

    query = "What is Tavily web search API?"
    try:
        result = adapter.search(query=query, max_results=int(env["WEB_SEARCH_MAX_RESULTS"]))
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL search", type(exc).__name__, file=sys.stderr)
        return 1

    if not result.hits:
        print("SMOKE_FAIL empty hits", file=sys.stderr)
        return 1

    step = build_public_web_search_step(result, api_key=env["WEB_SEARCH_API_KEY"])
    blob = json.dumps(step, ensure_ascii=False)
    secret = env["WEB_SEARCH_API_KEY"]
    if secret and secret in blob:
        print("SMOKE_FAIL api_key leaked in public step", file=sys.stderr)
        return 1
    if "api_key" in blob.lower() and '"api_key"' in blob:
        print("SMOKE_FAIL api_key field in public step", file=sys.stderr)
        return 1
    if step.get("kind") != "web_search" or step.get("label") != "网络检索":
        print("SMOKE_FAIL public step kind/label", file=sys.stderr)
        return 1
    if query not in blob and step.get("query") != query:
        print("SMOKE_FAIL query missing from public step", file=sys.stderr)
        return 1

    first = result.hits[0]
    print("hits", len(result.hits))
    print("first_title_len", len(first.title or ""))
    print("first_url_host", (first.url or "").split("/")[2] if "://" in (first.url or "") else "")
    print("public_step_ok", True)
    print("no_fake", "TavilyHttpWebSearchAdapter")
    print("S23_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
