"""Unique HTTP composition root for the support platform."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from support_platform.api.conversations import router as conversations_router
from support_platform.api.investigations import router as investigations_router
from support_platform.api import investigations as inv_api
from support_platform.auth.phase1_guard import unauthenticated_phase1_status
from support_platform.auth.routes import router as auth_router
from support_platform.task_runtime.routes import router as v2_investigations_router
from support_platform.web import router as workbench_router

_PKG = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Wire real InvestigationService + conversation turn for official uvicorn."""
    from support_platform.api import conversations as conv_api
    from support_platform.composition import (
        load_dotenv,
        try_wire_conversation_turn,
        try_wire_investigation_service,
    )

    load_dotenv()
    try_wire_investigation_service()
    try_wire_conversation_turn()
    try:
        yield
    finally:
        inv_api.reset_investigation_service()
        conv_api.reset_conversation_turn()


app = FastAPI(title="support-platform", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(v2_investigations_router)
app.include_router(conversations_router)
app.include_router(investigations_router)
app.include_router(workbench_router)
app.mount(
    "/static",
    StaticFiles(directory=str(_PKG / "static")),
    name="static",
)


@app.middleware("http")
async def restrict_phase1_api_when_disabled(request: Request, call_next):  # type: ignore[no-untyped-def]
    path = request.url.path
    if path == "/api/investigations" or path.startswith("/api/investigations/"):
        try:
            from support_platform.config import load_settings

            mode = load_settings().phase1_api_mode
        except Exception:  # noqa: BLE001 - missing env must not crash middleware path oddly
            mode = "open"
        reject = unauthenticated_phase1_status(mode)
        if reject is not None:
            return JSONResponse(
                status_code=reject,
                content={"detail": "phase1 api disabled for pilot"},
            )
    return await call_next(request)


@app.get("/health/live")
def health_live() -> JSONResponse:
    """Process liveness only — does not touch the database."""
    return JSONResponse(content={"status": "alive"}, status_code=200)


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    """Readiness: config + PostgreSQL + Redis (SPEC §4 Phase 2). Never use live as ready."""
    from support_platform.config import load_settings
    from support_platform.deploy.health import probe_readiness

    try:
        settings = load_settings()
    except Exception:  # noqa: BLE001 - missing/invalid config → not ready
        return JSONResponse(content={"status": "not_ready"}, status_code=503)

    result = probe_readiness(
        database_url=str(settings.database_url),
        redis_url=settings.redis_url,
    )
    if not result.get("ok"):
        return JSONResponse(content={"status": "not_ready"}, status_code=503)
    return JSONResponse(content={"status": "ready"}, status_code=200)
