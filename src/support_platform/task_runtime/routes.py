"""OP-07 / OP-08 / OP-09 / OP-10 / OP-11: v2 investigations (SPEC §4)."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from support_platform.auth import CSRF_HEADER, SESSION_COOKIE
from support_platform.auth.access import investigation_access_status
from support_platform.auth.store import get_session, get_user
from support_platform.config import load_settings
from support_platform.investigation import hitl
from support_platform.task_runtime import events as event_svc
from support_platform.task_runtime import service as task_svc

router = APIRouter(tags=["v2-investigations"])


class CreateInvestigationBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] | None = None


class ResumeBody(BaseModel):
    interrupt_id: str = Field(min_length=1, max_length=256)
    action: Literal["SUPPLY", "APPROVE", "EDIT", "REJECT"]
    input: dict[str, Any] | None = None


class CancelBody(BaseModel):
    reason: str | None = None


def _require_session(request: Request):
    session = get_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        return None, JSONResponse(status_code=401, content={"detail": "not authenticated"})
    return session, None


def _require_csrf(request: Request, session) -> JSONResponse | None:
    csrf = request.headers.get(CSRF_HEADER)
    if not csrf or csrf != session.csrf_token:
        return JSONResponse(status_code=403, content={"detail": "csrf required"})
    return None


@router.post("/api/v2/investigations")
def create_investigation(body: CreateInvestigationBody, request: Request) -> JSONResponse:
    session, err = _require_session(request)
    if err is not None:
        return err
    csrf_err = _require_csrf(request, session)
    if csrf_err is not None:
        return csrf_err

    idem = (request.headers.get("Idempotency-Key") or "").strip()
    if not idem:
        return JSONResponse(status_code=422, content={"detail": "Idempotency-Key required"})

    user = get_user(session.user_id)
    if user is None or not user.is_active:
        return JSONResponse(status_code=401, content={"detail": "not authenticated"})
    if user.role == "KNOWLEDGE_ADMIN":
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    settings = load_settings()
    database_url = str(settings.database_url)
    bh = task_svc.body_hash_for_create(question=body.question, context=body.context)
    prior = task_svc.lookup_create_idempotency(
        database_url=database_url,
        owner_user_id=user.id,
        idempotency_key=idem,
    )
    if prior is not None:
        if prior["body_hash"] != bh:
            return JSONResponse(status_code=409, content={"detail": "idempotency key conflict"})
        return JSONResponse(
            status_code=202,
            content={"id": prior["investigation_id"], "task_status": "QUEUED"},
        )

    investigation_id = uuid4()
    task_svc.create_queued_investigation(
        database_url=database_url,
        investigation_id=investigation_id,
        question=body.question,
        context=body.context or {},
        owner_user_id=user.id,
        knowledge_version_ids=[],
        config_versions={},
    )
    task_svc.remember_create_idempotency(
        database_url=database_url,
        owner_user_id=user.id,
        idempotency_key=idem,
        body_hash=bh,
        investigation_id=investigation_id,
    )
    return JSONResponse(
        status_code=202,
        content={"id": str(investigation_id), "task_status": "QUEUED"},
    )


@router.get("/api/v2/investigations/{investigation_id}")
def get_investigation(investigation_id: UUID, request: Request) -> JSONResponse:
    session, err = _require_session(request)
    if err is not None:
        return err

    user = get_user(session.user_id)
    if user is None or not user.is_active:
        return JSONResponse(status_code=401, content={"detail": "not authenticated"})

    settings = load_settings()
    task = task_svc.get_task(
        database_url=str(settings.database_url),
        investigation_id=investigation_id,
    )
    if task is None or not task.get("owner_user_id"):
        return JSONResponse(status_code=404, content={"detail": "not found"})

    status = investigation_access_status(
        viewer_user_id=str(user.id),
        owner_user_id=str(task["owner_user_id"]),
        viewer_role=user.role,
        action="read",
    )
    if status != 200:
        return JSONResponse(status_code=status, content={"detail": "not found" if status == 404 else "forbidden"})

    return JSONResponse(
        status_code=200,
        content={
            "id": task["id"],
            "task_status": task["task_status"],
            "result_status": task.get("result_status"),
            "public_steps": task.get("public_steps") or [],
            "output": task.get("output"),
            "error_code": task.get("error_code"),
            "question": task.get("question"),
            "knowledge_snapshot_hash": task.get("knowledge_snapshot_hash"),
            "prompt_version": task.get("prompt_version"),
            "model_config_version": task.get("model_config_version"),
            "retrieval_config_version": task.get("retrieval_config_version"),
            "active_version_ids": task.get("active_version_ids") or [],
        },
    )


def _owner_task_or_error(
    *,
    investigation_id: UUID,
    user,
    database_url: str,
    action: str = "write",
):
    task = task_svc.get_task(database_url=database_url, investigation_id=investigation_id)
    if task is None or not task.get("owner_user_id"):
        return None, JSONResponse(status_code=404, content={"detail": "not found"})
    status = investigation_access_status(
        viewer_user_id=str(user.id),
        owner_user_id=str(task["owner_user_id"]),
        viewer_role=user.role,
        action=action,
    )
    if status != 200:
        return None, JSONResponse(
            status_code=status,
            content={"detail": "not found" if status == 404 else "forbidden"},
        )
    return task, None


@router.post("/api/v2/investigations/{investigation_id}/resume")
def resume_investigation(
    investigation_id: UUID,
    body: ResumeBody,
    request: Request,
) -> JSONResponse:
    """OP-10: resume from INTERRUPTED with matching interrupt_id + human action."""
    session, err = _require_session(request)
    if err is not None:
        return err
    csrf_err = _require_csrf(request, session)
    if csrf_err is not None:
        return csrf_err

    idem = (request.headers.get("Idempotency-Key") or "").strip()
    if not idem:
        return JSONResponse(status_code=422, content={"detail": "Idempotency-Key required"})

    user = get_user(session.user_id)
    if user is None or not user.is_active:
        return JSONResponse(status_code=401, content={"detail": "not authenticated"})
    if user.role == "KNOWLEDGE_ADMIN":
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    settings = load_settings()
    database_url = str(settings.database_url)
    _, access_err = _owner_task_or_error(
        investigation_id=investigation_id,
        user=user,
        database_url=database_url,
        action="write",
    )
    if access_err is not None:
        return access_err

    try:
        result = hitl.apply_human_decision(
            database_url=database_url,
            investigation_id=investigation_id,
            interrupt_id=body.interrupt_id,
            action=body.action,
            input_payload=body.input,
            idempotency_key=idem,
        )
    except hitl.ConflictError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc) or "conflict"})
    except LookupError:
        return JSONResponse(status_code=404, content={"detail": "not found"})
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    return JSONResponse(status_code=202, content=result)


@router.post("/api/v2/investigations/{investigation_id}/cancel")
def cancel_investigation(
    investigation_id: UUID,
    request: Request,
    body: CancelBody | None = None,
) -> JSONResponse:
    """OP-11: collaborative cancel; does not erase evidence."""
    session, err = _require_session(request)
    if err is not None:
        return err
    csrf_err = _require_csrf(request, session)
    if csrf_err is not None:
        return csrf_err

    idem = (request.headers.get("Idempotency-Key") or "").strip()
    if not idem:
        return JSONResponse(status_code=422, content={"detail": "Idempotency-Key required"})

    user = get_user(session.user_id)
    if user is None or not user.is_active:
        return JSONResponse(status_code=401, content={"detail": "not authenticated"})
    if user.role == "KNOWLEDGE_ADMIN":
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    settings = load_settings()
    database_url = str(settings.database_url)
    _, access_err = _owner_task_or_error(
        investigation_id=investigation_id,
        user=user,
        database_url=database_url,
        action="write",
    )
    if access_err is not None:
        return access_err

    _ = body
    try:
        result = hitl.request_cancel(
            database_url=database_url,
            investigation_id=investigation_id,
            idempotency_key=idem,
        )
    except LookupError:
        return JSONResponse(status_code=404, content={"detail": "not found"})

    return JSONResponse(status_code=202, content=result)


@router.get(
    "/api/v2/investigations/{investigation_id}/events",
    response_model=None,
)
def stream_investigation_events(
    investigation_id: UUID,
    request: Request,
    snapshot: int = 0,
) -> Any:
    """OP-09: authorized text/event-stream; Last-Event-ID catch-up from PostgreSQL."""
    session, err = _require_session(request)
    if err is not None:
        return err

    user = get_user(session.user_id)
    if user is None or not user.is_active:
        return JSONResponse(status_code=401, content={"detail": "not authenticated"})

    settings = load_settings()
    database_url = str(settings.database_url)
    task, access_err = _owner_task_or_error(
        investigation_id=investigation_id,
        user=user,
        database_url=database_url,
        action="read",
    )
    if access_err is not None:
        return access_err
    _ = task

    raw_last = request.headers.get("Last-Event-ID") or request.query_params.get(
        "last_event_id"
    )
    try:
        last_id = int(raw_last) if raw_last is not None and str(raw_last).strip() != "" else 0
    except ValueError:
        last_id = -1

    iid = str(investigation_id)

    def _generate():
        stale = last_id < 0 or (
            last_id > 0
            and event_svc.is_stale_last_event_id(
                database_url=database_url,
                investigation_id=iid,
                last_event_id=last_id,
            )
        )
        if stale:
            ctrl = {
                "detail": "stale Last-Event-ID; re-read OP-08",
                "op08": f"/api/v2/investigations/{iid}",
                "reload": True,
                "gone": True,
            }
            yield event_svc.format_sse_frame(
                sequence=0,
                event_type="gone",
                data=ctrl,
            )
            return

        rows = event_svc.list_events_after(
            database_url=database_url,
            investigation_id=iid,
            last_event_id=max(last_id, 0),
        )
        for row in rows:
            yield event_svc.format_sse_frame(
                sequence=int(row["sequence"]),
                event_type=str(row["type"]),
                data=dict(row.get("public_payload") or {}),
            )
        _ = snapshot

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
