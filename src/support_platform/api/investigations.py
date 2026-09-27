"""Investigation HTTP adapters — OP-01～04 thin routes over InvestigationService."""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class NoAvailableKnowledgeError(Exception):
    """Raised when no READY knowledge can serve a new investigation."""


class InvestigationDependencyError(Exception):
    """Raised after an investigation id exists but a required dependency fails."""

    def __init__(
        self,
        *,
        investigation_id: str,
        error_code: str,
        public_steps: list[dict[str, Any]],
    ) -> None:
        super().__init__(error_code)
        self.investigation_id = investigation_id
        self.error_code = error_code
        self.public_steps = list(public_steps)


class InvestigationService(Protocol):
    def create_investigation(
        self, *, question: str, context: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def get_investigation(self, investigation_id: str) -> dict[str, Any] | None: ...

    def get_evidence(
        self, investigation_id: str, evidence_id: str
    ) -> dict[str, Any] | None: ...


_service: InvestigationService | None = None

router = APIRouter(prefix="/api/investigations", tags=["investigations"])

_ALLOWED_FEEDBACK = frozenset({"ADOPTED", "EDITED", "ESCALATED"})


def set_investigation_service(service: InvestigationService) -> None:
    global _service
    _service = service


def reset_investigation_service() -> None:
    global _service
    _service = None


def get_investigation_service() -> InvestigationService | None:
    return _service


class CreateInvestigationBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] | None = None


class FeedbackBody(BaseModel):
    action: str
    reason: str | None = None
    edited_text: str | None = None


def _require_service() -> InvestigationService:
    svc = get_investigation_service()
    if svc is None:
        raise RuntimeError("investigation service not configured")
    return svc


@router.post("")
@router.post("/")
def create_investigation(body: CreateInvestigationBody) -> JSONResponse:
    question = body.question.strip()
    if not question:
        return JSONResponse(status_code=422, content={"detail": "question must not be empty"})
    svc = _require_service()
    try:
        result = svc.create_investigation(question=question, context=body.context)
    except NoAvailableKnowledgeError as exc:
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc) or "no available knowledge"},
        )
    except InvestigationDependencyError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "id": exc.investigation_id,
                "task_status": "FAILED",
                "result_status": None,
                "error_code": exc.error_code,
                "public_steps": list(exc.public_steps),
            },
        )
    return JSONResponse(status_code=200, content=result)


@router.get("/{investigation_id}")
def get_investigation(investigation_id: str) -> JSONResponse:
    svc = get_investigation_service()
    row = svc.get_investigation(investigation_id) if svc is not None else None
    if row is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": "investigation not found",
                "error_code": "INVESTIGATION_NOT_FOUND",
            },
        )
    return JSONResponse(status_code=200, content=row)


@router.get("/{investigation_id}/evidence/{evidence_id}")
def get_evidence(investigation_id: str, evidence_id: str) -> JSONResponse:
    svc = get_investigation_service()
    if svc is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "evidence not found", "error_code": "EVIDENCE_NOT_FOUND"},
        )
    if svc.get_investigation(investigation_id) is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": "investigation not found",
                "error_code": "INVESTIGATION_NOT_FOUND",
            },
        )
    row = svc.get_evidence(investigation_id, evidence_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "evidence not found", "error_code": "EVIDENCE_NOT_FOUND"},
        )
    return JSONResponse(status_code=200, content=row)


@router.post("/{investigation_id}/feedback")
def post_feedback(investigation_id: str, body: FeedbackBody) -> JSONResponse:
    from support_platform.feedback.store import append_feedback

    if body.action not in _ALLOWED_FEEDBACK:
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid feedback action: {body.action!r}"},
        )

    svc = get_investigation_service()
    row = svc.get_investigation(investigation_id) if svc is not None else None
    if row is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": "investigation not found",
                "error_code": "INVESTIGATION_NOT_FOUND",
            },
        )
    if row.get("task_status") != "COMPLETED":
        return JSONResponse(
            status_code=409,
            content={"detail": "feedback not allowed for current investigation status"},
        )

    feedback = append_feedback(
        investigation_id=investigation_id,
        action=body.action,
        reason=body.reason,
        edited_text=body.edited_text,
    )
    return JSONResponse(
        status_code=200,
        content={"feedback_id": feedback["id"], "action": feedback["action"]},
    )
