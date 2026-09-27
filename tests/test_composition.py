"""Composition-root wiring: official uvicorn must attach RealInvestigationService."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


def test_build_real_investigation_service_requires_llm_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from support_platform import composition

    monkeypatch.setattr(composition, "load_dotenv", lambda path=None: None)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5432/db")
    with pytest.raises(RuntimeError, match="missing|LLM|NOT_EXECUTED"):
        composition.build_real_investigation_service()


def test_try_wire_sets_global_service_when_env_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from support_platform import composition
    from support_platform.api import investigations as inv_api
    from support_platform.api.real_service import RealInvestigationService

    inv_api.reset_investigation_service()
    fake = object()

    monkeypatch.setattr(
        composition,
        "build_real_investigation_service",
        lambda: fake,
    )
    # try_wire also best-effort cleans stale RUNNING via real PG — keep unit test offline.
    monkeypatch.setattr(
        composition,
        "require_real_runtime_env",
        lambda: {"DATABASE_URL": "postgresql+psycopg://unit:test@127.0.0.1:1/offline"},
    )
    monkeypatch.setattr(
        "support_platform.investigation.fail_stale_running",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "support_platform.investigation.pg_store.PgInvestigationStore",
        lambda *_a, **_k: object(),
    )
    assert composition.try_wire_investigation_service() is True
    assert inv_api.get_investigation_service() is fake
    inv_api.reset_investigation_service()
    assert isinstance(RealInvestigationService, type)


def test_main_lifespan_wires_service_via_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Official app lifespan must call try_wire so POST / is not 'not configured'."""
    from support_platform.api import investigations as inv_api
    from support_platform.main import app

    class StubSvc:
        def create_investigation(
            self, *, question: str, context: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            _ = context
            return {
                "id": str(uuid4()),
                "task_status": "COMPLETED",
                "result_status": "NO_EVIDENCE",
                "public_steps": [{"step": "stub"}],
                "claims": [],
                "evidence": [],
                "output": {},
                "error_code": None,
            }

        def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
            _ = investigation_id
            return None

        def get_evidence(
            self, investigation_id: str, evidence_id: str
        ) -> dict[str, Any] | None:
            _ = investigation_id, evidence_id
            return None

    stub = StubSvc()

    def _wire() -> bool:
        inv_api.set_investigation_service(stub)
        return True

    monkeypatch.setattr(
        "support_platform.composition.try_wire_investigation_service",
        _wire,
    )
    monkeypatch.setattr(
        "support_platform.composition.try_wire_conversation_turn",
        lambda: False,
    )
    inv_api.reset_investigation_service()

    with TestClient(app) as client:
        assert inv_api.get_investigation_service() is stub
        home = client.get("/")
        assert home.status_code == 200
        posted = client.post("/", data={"question": "composition wire check"})
        assert posted.status_code == 200
        assert b"not configured" not in posted.content.lower()
        assert b"Internal Server Error" not in posted.content


def test_run_investigation_reuses_existing_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from support_platform.investigation import run_investigation

    fixed_id = str(uuid4())
    saved: dict[str, Any] = {}

    class MemStore:
        def create_running(self, *, question: str, frozen: dict | None = None) -> str:
            raise AssertionError("must not create_running when investigation_id given")

        def get(self, investigation_id: str) -> dict[str, Any]:
            if investigation_id in saved:
                return dict(saved[investigation_id])
            return {
                "id": investigation_id,
                "question": "q",
                "task_status": "RUNNING",
                "result_status": None,
                "public_steps": [],
                "error_code": None,
                "output": {},
            }

        def save(self, row: dict[str, Any]) -> None:
            saved[row["id"]] = dict(row)

        def list_running(self) -> list:
            return []

    class FakeGraph:
        def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
            assert state["investigation_id"] == fixed_id
            return {
                "task_status": "COMPLETED",
                "result_status": "NO_EVIDENCE",
                "public_steps": [{"step": "retrieve"}],
                "hits": [],
                "error_code": None,
            }

    monkeypatch.setattr(
        "support_platform.investigation.build_investigation_graph",
        lambda **_: FakeGraph(),
    )
    row = run_investigation(
        question="reuse id",
        retrieve=lambda *_a, **_k: [],
        chat=object(),
        store=MemStore(),
        investigation_id=fixed_id,
    )
    assert row["id"] == fixed_id
    assert row["task_status"] == "COMPLETED"


def test_worker_real_mode_runs_investigation_not_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from support_platform.task_runtime import worker as worker_mod

    iid = str(uuid4())
    called: dict[str, Any] = {}

    monkeypatch.setattr(worker_mod, "_mark_task_running", lambda **_: None)
    monkeypatch.setattr(
        worker_mod,
        "get_task",
        lambda **kw: {
            "id": str(kw["investigation_id"]),
            "question": "worker real q",
            "task_status": "QUEUED",
            "result_status": None,
            "output": None,
            "public_steps": [],
        },
    )
    monkeypatch.setattr(
        worker_mod,
        "count_final_outputs",
        lambda **_: 0,
    )

    def _fake_run(**kwargs: Any) -> dict[str, Any]:
        called.update(kwargs)
        return {
            "id": str(kwargs["investigation_id"]),
            "task_status": "COMPLETED",
            "result_status": "ANSWERED",
            "public_steps": [{"step": "real_path"}],
            "output": {"hit_chunk_ids": ["c1"]},
            "error_code": None,
        }

    monkeypatch.setattr(worker_mod, "run_queued_investigation", _fake_run)

    def _persist(**kwargs: Any) -> dict[str, Any]:
        return {
            "accepted": True,
            "duplicate": False,
            "task_status": "COMPLETED",
            "result_status": kwargs["result_status"],
            "output": kwargs["output"],
            "public_steps": kwargs["public_steps"],
        }

    monkeypatch.setattr(worker_mod, "persist_final_output", _persist)

    out = worker_mod.process_investigation(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
        investigation_id=iid,
        worker_id="w-real",
        mode="real",
    )
    assert called.get("investigation_id") == iid
    assert called.get("question") == "worker real q"
    assert out["result_status"] == "ANSWERED"
    assert "deterministic" not in str(out.get("output"))
