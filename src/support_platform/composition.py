"""Production composition root helpers — wire real adapters for official entrypoints.

Fake/mock adapters must not be assembled here. Tests may monkeypatch these functions.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from support_platform.api import investigations as inv_api
from support_platform.api.real_service import RealInvestigationService

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None) -> None:
    """Load key=value pairs into os.environ without overriding existing values."""
    env_path = path or (_REPO_ROOT / ".env")
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def require_real_runtime_env() -> dict[str, str]:
    """Require DATABASE_URL + Chat + Embedding for real investigation wiring."""
    load_dotenv()
    required = [
        "DATABASE_URL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSION",
    ]
    missing = [k for k in required if not (os.environ.get(k) or "").strip()]
    if missing:
        raise RuntimeError(f"NOT_EXECUTED missing env: {', '.join(missing)}")
    if "fake" in os.environ["LLM_BASE_URL"].lower():
        raise RuntimeError("SMOKE_FAIL Fake LLM URL refused")
    return {k: os.environ[k].strip() for k in required}


def build_retrieve(env: dict[str, str], embedding: Any):
    from support_platform.search.pg import retrieve_pg

    def retrieve(query: str, **_: object):
        result = retrieve_pg(
            query=query,
            database_url=env["DATABASE_URL"],
            embedding=embedding,
            top_k=10,
            rrf_k=60,
        )
        return list(result.merged)

    return retrieve


def build_real_investigation_service() -> RealInvestigationService:
    """Assemble RealInvestigationService from process env (no Fake adapters)."""
    from support_platform.infrastructure.chat import HttpChatAdapter
    from support_platform.infrastructure.embedding import HttpEmbeddingAdapter
    from support_platform.investigation.pg_store import PgInvestigationStore

    env = require_real_runtime_env()
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
        raise RuntimeError("SMOKE_FAIL Fake chat refused in composition root")
    store = PgInvestigationStore(env["DATABASE_URL"])
    retrieve = build_retrieve(env, embedding)
    return RealInvestigationService(
        database_url=env["DATABASE_URL"],
        store=store,
        retrieve=retrieve,
        chat=chat,
    )


def try_wire_investigation_service() -> bool:
    """Wire the global InvestigationService. Returns False if env incomplete."""
    try:
        service = build_real_investigation_service()
    except RuntimeError as exc:
        logger.warning("investigation service not wired: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - startup must not crash health
        logger.warning("investigation service wire failed: %s", type(exc).__name__)
        return False
    inv_api.set_investigation_service(service)
    from support_platform.investigation import fail_stale_running
    from support_platform.investigation.pg_store import PgInvestigationStore

    try:
        env = require_real_runtime_env()
        cleaned = fail_stale_running(PgInvestigationStore(env["DATABASE_URL"]))
        if cleaned:
            logger.info("fail_stale_running cleaned %s", len(cleaned))
    except Exception:  # noqa: BLE001 - cleanup is best-effort at startup
        logger.warning("fail_stale_running skipped", exc_info=False)
    return True


def try_wire_conversation_turn() -> bool:
    """Wire Phase4 conversation OP-15/16/17 with real Chat + Web Search + PG.

    Returns False if env incomplete so offline unit tests keep Fake defaults.
    Under pytest, skip unless WIRE_CONVERSATION_IN_TESTS=1 (avoids paid calls).
    """
    from support_platform.api import conversations as conv_api
    from support_platform.infrastructure.chat import HttpChatAdapter
    from support_platform.infrastructure.embedding import HttpEmbeddingAdapter
    from support_platform.infrastructure.web_search import TavilyHttpWebSearchAdapter
    from support_platform.infrastructure.web_search.smoke_s23 import (
        require_real_web_search_env,
    )
    from support_platform.investigation.phase4_planner import Phase4SequentialPlanner
    from support_platform.investigation.pg_store import PgInvestigationStore
    from support_platform.investigation.smoke_s4 import _ensure_corpus

    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get(
        "WIRE_CONVERSATION_IN_TESTS"
    ) != "1":
        logger.info("conversation turn wire skipped under pytest")
        return False

    try:
        env = require_real_runtime_env()
        web_env = require_real_web_search_env()
    except RuntimeError as exc:
        logger.warning("conversation turn not wired: %s", exc)
        return False

    try:
        _ensure_corpus(env["DATABASE_URL"])
    except Exception as exc:  # noqa: BLE001 - corpus best-effort; retrieve may still work
        logger.warning("conversation corpus seed failed: %s", type(exc).__name__)

    try:
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
            raise RuntimeError("Fake chat refused in conversation composition root")
        web = TavilyHttpWebSearchAdapter(
            base_url=web_env["WEB_SEARCH_BASE_URL"],
            api_key=web_env["WEB_SEARCH_API_KEY"],
            max_results=int(web_env["WEB_SEARCH_MAX_RESULTS"]),
            timeout_s=45.0,
        )
        if type(web).__name__ == "FakeWebSearchAdapter":
            raise RuntimeError("Fake web_search refused in conversation composition root")
        retrieve = build_retrieve(env, embedding)

        def _planner_factory(question: str) -> Phase4SequentialPlanner:
            return Phase4SequentialPlanner(chat=chat, question=question)

        conv_api.configure_conversation_turn(
            retrieve=retrieve,
            web_search=web,
            planner_factory=_planner_factory,
            store_factory=lambda: PgInvestigationStore(env["DATABASE_URL"]),
        )
    except Exception as exc:  # noqa: BLE001 - startup must not crash health
        logger.warning("conversation turn wire failed: %s", type(exc).__name__)
        conv_api.reset_conversation_turn()
        return False
    logger.info("conversation turn wired with real Chat + Web Search + PG")
    return True
