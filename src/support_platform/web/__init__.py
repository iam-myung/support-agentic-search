"""Page routes: Phase4 chat at / ; legacy workbench at /workbench."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from support_platform.api import investigations as inv_api

_PKG = Path(__file__).resolve().parent.parent  # support_platform/
TEMPLATES_DIR = _PKG / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["pages"])


def _require_service() -> inv_api.InvestigationService:
    svc = inv_api.get_investigation_service()
    if svc is None:
        raise RuntimeError("investigation service not configured")
    return svc


@router.get("/", response_class=HTMLResponse)
def chat_get(request: Request) -> HTMLResponse:
    """OP-14: official entry is conversation UI."""
    return templates.TemplateResponse(request, "chat.html", {})


@router.post("/")
def chat_post_redirect() -> RedirectResponse:
    """Legacy form POST / is no longer the main path — redirect to chat."""
    return RedirectResponse(url="/", status_code=302)


@router.get("/workbench", response_class=HTMLResponse)
def workbench_get(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "workbench.html",
        {"result": None, "failure": None, "form_error": None},
    )


@router.post("/workbench", response_class=HTMLResponse)
def workbench_post(
    request: Request,
    question: str = Form(""),
    error_code: str = Form(""),
    product_version: str = Form(""),
) -> HTMLResponse:
    q = (question or "").strip()
    if not q:
        return templates.TemplateResponse(
            request,
            "workbench.html",
            {
                "result": None,
                "failure": None,
                "form_error": "question must not be empty",
            },
            status_code=200,
        )

    context: dict[str, Any] = {}
    if error_code.strip():
        context["error_code"] = error_code.strip()
    if product_version.strip():
        context["product_version"] = product_version.strip()

    svc = _require_service()
    try:
        result = svc.create_investigation(question=q, context=context or None)
    except inv_api.NoAvailableKnowledgeError as exc:
        return templates.TemplateResponse(
            request,
            "workbench.html",
            {
                "result": None,
                "failure": {
                    "id": None,
                    "error_code": "NO_AVAILABLE_KNOWLEDGE",
                    "detail": str(exc) or "no available knowledge",
                    "public_steps": [],
                },
                "form_error": None,
            },
        )
    except inv_api.InvestigationDependencyError as exc:
        return templates.TemplateResponse(
            request,
            "workbench.html",
            {
                "result": None,
                "failure": {
                    "id": exc.investigation_id,
                    "error_code": exc.error_code,
                    "detail": "依赖失败",
                    "public_steps": list(exc.public_steps),
                    "task_status": "FAILED",
                },
                "form_error": None,
            },
        )

    return templates.TemplateResponse(
        request,
        "workbench.html",
        {"result": result, "failure": None, "form_error": None},
    )


@router.get(
    "/investigations/{investigation_id}/evidence/{evidence_id}",
    response_class=HTMLResponse,
)
def workbench_evidence(
    request: Request, investigation_id: str, evidence_id: str
) -> HTMLResponse:
    svc = inv_api.get_investigation_service()
    row = svc.get_evidence(investigation_id, evidence_id) if svc is not None else None
    if row is None:
        return templates.TemplateResponse(
            request,
            "evidence.html",
            {
                "found": False,
                "quote_snapshot": None,
                "source_title_snapshot": None,
                "investigation_id": investigation_id,
                "evidence_id": evidence_id,
            },
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "evidence.html",
        {
            "found": True,
            "quote_snapshot": row.get("quote_snapshot"),
            "source_title_snapshot": row.get("source_title_snapshot"),
            "locator_snapshot": row.get("locator_snapshot"),
            "investigation_id": investigation_id,
            "evidence_id": evidence_id,
        },
    )
