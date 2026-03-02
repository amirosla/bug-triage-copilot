"""FastAPI application entry point."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import structlog
from fastapi import FastAPI, Request, Response

from core.config import settings
from apps.api.routes import webhooks, api as api_router, ui as ui_router

# ── Structured logging setup ──────────────────────────────────────────────────

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, settings.log_level.upper(), logging.INFO)
    ),
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)

log = structlog.get_logger()

# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Bug Triage Copilot",
    description="Automated GitHub Issue triage with LLM analysis and semantic search.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


# ── Middleware ────────────────────────────────────────────────────────────────


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
    """Inject correlation_id (GitHub delivery id or generated) into log context."""
    correlation_id = request.headers.get("X-GitHub-Delivery", "")
    start = time.perf_counter()

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        correlation_id=correlation_id,
        method=request.method,
        path=request.url.path,
    )

    response: Response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    log.info("request", status=response.status_code, duration_ms=duration_ms)
    return response


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(webhooks.router)
app.include_router(api_router.router, prefix="/api")
app.include_router(ui_router.router)


# ── Health check ──────────────────────────────────────────────────────────────


@app.get("/health", tags=["ops"], summary="Health check")
def health_check() -> dict[str, str]:
    """Returns 200 if the service is up."""
    return {"status": "ok", "service": "bug-triage-copilot-api"}
