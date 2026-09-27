"""Thin Phase4 conversation HTTP API (OP-15 / OP-16 / OP-17)."""

from __future__ import annotations

from typing import Any, Callable
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from support_platform.conversation import run_conversation_turn
from support_platform.infrastructure.web_search import FakeWebSearchAdapter

router = APIRouter(prefix="/api/v2", tags=["conversations"])

_STORE: dict[str, dict[str, Any]] = {}

# Optional real-path injection for approved SMOKE (tests keep Fake defaults).
_TURN_OVERRIDE: dict[str, Any] | None = None


def configure_conversation_turn(
    *,
    retrieve: Callable[..., Any],
    web_search: Any,
    planner: Any | None = None,
    planner_factory: Callable[[str], Any] | None = None,
    store_factory: Callable[[], Any],
) -> None:
    """Inject real adapters for official SMOKE/live UI; call reset after."""
    if planner is None and planner_factory is None:
        raise ValueError("planner or planner_factory required")
    global _TURN_OVERRIDE
    _TURN_OVERRIDE = {
        "retrieve": retrieve,
        "web_search": web_search,
        "planner": planner,
        "planner_factory": planner_factory,
        "store_factory": store_factory,
    }


def reset_conversation_turn() -> None:
    global _TURN_OVERRIDE
    _TURN_OVERRIDE = None


class _CreateBody(BaseModel):
    title: str | None = None


class _MessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class _MemoryInvestigationStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def create_running(self, *, question: str) -> str:
        iid = str(uuid4())
        self.rows[iid] = {
            "id": iid,
            "question": question,
            "task_status": "RUNNING",
            "result_status": None,
            "public_steps": [],
            "error_code": None,
            "output": None,
        }
        return iid

    def save(self, row: dict[str, Any]) -> None:
        self.rows[row["id"]] = dict(row)

    def get(self, investigation_id: str) -> dict[str, Any]:
        return dict(self.rows[investigation_id])

    def list_running(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.rows.values() if r["task_status"] == "RUNNING"]


class _DefaultPlanner:
    """Deterministic offline planner for API/UI tests without paid providers."""

    def __init__(self) -> None:
        self._phase = 0

    def next_action(self, state: dict[str, Any]) -> dict[str, Any]:
        self._phase += 1
        if self._phase == 1:
            return {
                "type": "tool",
                "tool": "local_retrieve",
                "query": str(state.get("question") or ""),
            }
        return {"type": "synthesize"}

    def synthesize(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "result_status": "ANSWERED",
            "summary": "根据本地知识给出的处理建议。",
            "knowledge_basis": "LOCAL",
            "claims": [{"text": "本地建议", "provenance": "LOCAL"}],
        }


def _retrieve(query: str, **_: Any) -> list[Any]:
    class _Hit:
        chunk_id = "ui-local-1"
        content = f"Local note for: {query}"

    return [_Hit()]


def _run_turn(content: str) -> dict[str, Any]:
    if _TURN_OVERRIDE is not None:
        factory = _TURN_OVERRIDE.get("planner_factory")
        planner = factory(content) if callable(factory) else _TURN_OVERRIDE["planner"]
        return run_conversation_turn(
            content=content,
            retrieve=_TURN_OVERRIDE["retrieve"],
            web_search=_TURN_OVERRIDE["web_search"],
            planner=planner,
            store=_TURN_OVERRIDE["store_factory"](),
        )
    return run_conversation_turn(
        content=content,
        retrieve=_retrieve,
        web_search=FakeWebSearchAdapter(hits=[]),
        planner=_DefaultPlanner(),
        store=_MemoryInvestigationStore(),
    )


@router.post("/conversations", status_code=201)
def create_conversation(body: _CreateBody | None = None) -> JSONResponse:
    cid = str(uuid4())
    _STORE[cid] = {
        "conversation_id": cid,
        "title": (body.title if body else None) or "",
        "messages": [],
        "public_steps": [],
        "output": {},
        "knowledge_basis": None,
        "summary": "",
    }
    return JSONResponse(
        status_code=201,
        content={"conversation_id": cid, "id": cid},
    )


@router.post("/conversations/{conversation_id}/messages")
def post_message(
    conversation_id: str,
    body: _MessageBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    _ = request
    _ = idempotency_key
    row = _STORE.get(conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="content required")

    row["messages"].append({"role": "user", "content": content})

    try:
        turn = _run_turn(content)
    except Exception as exc:  # noqa: BLE001 - surface infra failure to UI
        # Drop the optimistic user message if turn could not run.
        if row["messages"] and row["messages"][-1].get("role") == "user":
            row["messages"].pop()
        detail = "conversation turn failed"
        name = type(exc).__name__
        msg = str(exc)
        if "ConnectionTimeout" in name or "ConnectionTimeout" in msg or "timeout" in msg.lower():
            detail = "database unavailable (connection timeout); start Docker Desktop / PostgreSQL"
        elif "OperationalError" in name or "OperationalError" in msg:
            detail = "database unavailable; start Docker Desktop / PostgreSQL"
        raise HTTPException(status_code=503, detail=detail) from exc

    output = dict(turn.get("output") or {})
    summary = str(output.get("summary") or "").strip()
    knowledge_basis = turn.get("knowledge_basis") or output.get("knowledge_basis")
    steps = list(turn.get("public_steps") or [])

    assistant = {
        "role": "assistant",
        "content": summary,
        "public_steps": steps,
        "output": output,
        "knowledge_basis": knowledge_basis,
        "investigation_id": turn.get("investigation_id"),
        "result_status": turn.get("result_status"),
        "task_status": turn.get("task_status"),
    }
    row["messages"].append(assistant)
    row["public_steps"] = steps
    row["output"] = output
    row["knowledge_basis"] = knowledge_basis
    row["summary"] = summary

    return JSONResponse(
        status_code=202,
        content={
            "conversation_id": conversation_id,
            "task_status": turn.get("task_status") or "COMPLETED",
            "accepted": True,
        },
    )


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict[str, Any]:
    row = _STORE.get(conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {
        "conversation_id": conversation_id,
        "id": conversation_id,
        "messages": list(row.get("messages") or []),
        "public_steps": list(row.get("public_steps") or []),
        "output": dict(row.get("output") or {}),
        "knowledge_basis": row.get("knowledge_basis"),
        "summary": row.get("summary") or "",
    }
