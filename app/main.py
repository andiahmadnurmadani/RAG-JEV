"""FastAPI application (PRD 20, 27, 29, 36).

* one error envelope for every failure (PRD 29);
* ``request_id`` on every response and in every log line (PRD 36);
* per-request observability fields: organization_id, application_id, latency,
  route, retrieval/reranker counts, model, status.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import build_services, services_from_request, set_services
from app.api.routes import auth, extract, knowledge, query, search, settings as settings_routes, system, unanswered
from app.core.bootstrap import ensure_bootstrap_admin
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger, request_log
from app.core.metrics import incr, observe, snapshot
from app.core.security import redact
from app.core.settings_store import apply_overrides, read_overrides

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    overlay = read_overrides(Path(settings.settings_override_path))
    if overlay:
        logger.info("runtime settings overrides applied: %s", ",".join(apply_overrides(settings, overlay)))
    # Satu kunci admin pertama bila belum ada kunci sama sekali; tanpa ini gerbang kode akses
    # tidak bisa dipasang pada pemasangan baru (lihat app/core/bootstrap.py).
    ensure_bootstrap_admin(settings)
    services = build_services(settings)
    app.state.services = services
    set_services(services)
    await services.startup()
    logger.info(
        "rag service started env=%s collection=%s embedder=%s reranker=%s llm=%s jev=%s",
        settings.app_env,
        settings.qdrant_collection,
        settings.embedding_provider,
        settings.reranker_provider,
        settings.llm_provider,
        settings.jev_mode,
    )
    try:
        yield
    finally:
        await services.shutdown()
        logger.info("rag service stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Multi-Tenant RAG & Jev AI Service",
        version="1.0.0",
        description=(
            "Multi-tenant knowledge indexing, hybrid retrieval and grounded generation. "
            "Tenant comes from the trusted context; Jev orchestrates capability only."
        ),
        lifespan=lifespan,
    )

    # Opt-in CORS (empty by default): the test console is served same-origin at /ui, so
    # cross-origin access stays closed unless an operator asks for it explicitly.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-Id", "X-Tenant-Context"],
            expose_headers=["X-Request-Id"],
        )

    ui_dir = Path(__file__).resolve().parent / "ui"
    if ui_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    # Situs dokumentasi (MkDocs) — hanya dipasang bila hasil build-nya ada.
    # Bangun dengan: bash scripts/build_docs.sh   → site/
    docs_dir = Path(settings.docs_site_dir)
    if settings.docs_site_dir and docs_dir.is_dir():
        app.mount("/guide", StaticFiles(directory=str(docs_dir), html=True), name="guide")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    prefix = settings.api_prefix
    app.include_router(auth.router, prefix=prefix)
    app.include_router(system.router, prefix=prefix)
    app.include_router(knowledge.router, prefix=prefix)
    app.include_router(query.router, prefix=prefix)
    app.include_router(search.router, prefix=prefix)
    app.include_router(extract.router, prefix=prefix)
    app.include_router(settings_routes.router, prefix=prefix)
    app.include_router(unanswered.router, prefix=prefix)

    @app.middleware("http")
    async def observability(request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex[:16]}"
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except AppError as exc:  # raised by middleware dependencies
            response = _error_response(exc, request_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("unhandled error: %s", redact(str(exc)))
            response = _error_response(AppError("INTERNAL_ERROR", "Unexpected internal error"), request_id)
        elapsed = time.perf_counter() - started
        observe("http_request_latency", elapsed)
        incr("http_requests_total")
        response.headers["X-Request-Id"] = request_id
        context = getattr(request.state, "tenant_context", None)
        status_code = getattr(response, "status_code", 500)
        if status_code >= 500:
            incr("http_errors_total")
        request_log(
            {
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": status_code,
                "latency_ms": round(elapsed * 1000, 2),
                "organization_id": getattr(context, "organization_id", None),
                "application_id": getattr(context, "application_id", None),
                "user_id": getattr(context, "user_id", None),
                "metrics": snapshot().get("counters", {}),
            }
        )
        # Aset statis (UI + situs dokumentasi) tidak punya hash isi di namanya, jadi
        # max-age panjang membuat peramban menjalankan berkas lama setelah redeploy —
        # HTML baru bertemu JS lama, tombol yang sudah tidak ada jadi null dan halaman
        # melempar TypeError. "no-cache" tetap memakai ETag/Last-Modified: bila berkas
        # tidak berubah jawabannya 304, jadi tidak ada tambahan lalu lintas yang berarti.
        path = request.url.path
        if path == "/ui" or path.startswith(("/ui/", "/guide/")):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc, getattr(request.state, "request_id", "req_unknown"))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        error = AppError(
            "VALIDATION_ERROR",
            "Request validation failed",
            details={"errors": _clean_validation_errors(exc.errors())},
        )
        return _error_response(error, getattr(request.state, "request_id", "req_unknown"))

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "KNOWLEDGE_NOT_FOUND" if exc.status_code == 404 else "INTERNAL_ERROR"
        if exc.status_code == 401:
            code = "AUTH_INVALID"
        if exc.status_code == 403:
            code = "AUTH_FORBIDDEN"
        if exc.status_code == 429:
            code = "RATE_LIMITED"
        error = AppError(code, str(exc.detail), status_code=exc.status_code)
        return _error_response(error, getattr(request.state, "request_id", "req_unknown"))

    return app


def _error_response(error: AppError, request_id: str) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.envelope(request_id))


def _clean_validation_errors(errors: Any) -> Any:
    cleaned = []
    for item in errors or []:
        entry: Dict[str, Any] = {
            "loc": [str(part) for part in item.get("loc", [])],
            "msg": str(item.get("msg", "")),
            "type": str(item.get("type", "")),
        }
        cleaned.append(entry)
    return cleaned


app = create_app()


def run() -> None:
    """``python -m app.main`` entrypoint (uvicorn)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
