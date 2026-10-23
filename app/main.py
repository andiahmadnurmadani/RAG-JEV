"""FastAPI application (PRD 20, 27, 29, 36).

* one error envelope for every failure (PRD 29);
* ``request_id`` on every response and in every log line (PRD 36);
* per-request observability fields: organization_id, application_id, latency,
  route, retrieval/reranker counts, model, status.
"""

from __future__ import annotations

import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
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
from app.core import site_gate

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

    # Password situs untuk halaman web (didaftarkan sebelum observability, jadi berjalan di
    # dalamnya: respons halaman login tetap mendapat X-Request-Id dan header keamanan).
    gate = site_gate.SiteGate(settings)

    @app.middleware("http")
    async def site_password_gate(request: Request, call_next):
        path = request.url.path
        if not gate.enabled or gate.is_exempt(path):
            return await call_next(request)

        if path == site_gate.LOGIN_PATH:
            if request.method == "POST":
                return await _site_login(request)
            if gate.valid(request.cookies.get(site_gate.COOKIE_NAME)):
                return RedirectResponse(site_gate.safe_next(request.query_params.get("next")), status_code=303)
            return _login_page(site_gate.safe_next(request.query_params.get("next")))
        if path == site_gate.LOGOUT_PATH:
            response = RedirectResponse(site_gate.LOGIN_PATH, status_code=303)
            response.delete_cookie(site_gate.COOKIE_NAME, path="/")
            return response

        if gate.valid(request.cookies.get(site_gate.COOKIE_NAME)):
            return await call_next(request)
        if request.method in ("GET", "HEAD"):
            return RedirectResponse(site_gate.login_redirect_target(path, request.url.query), status_code=303)
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": {"code": "AUTH_INVALID", "message": "Password situs diperlukan"}},
        )

    def _login_page(next_path: str, error: str = "", status_code: int = 200) -> HTMLResponse:
        return HTMLResponse(
            site_gate.login_page(next_path, error),
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    async def _site_login(request: Request):
        form = site_gate.parse_form(await request.body())
        next_path = site_gate.safe_next(form.get("next"))
        client = auth._client(request)
        blocked = max(
            site_gate.THROTTLE.blocked_for(client),
            site_gate.GLOBAL_THROTTLE.blocked_for(site_gate.GLOBAL_KEY),
        )
        if blocked > 0:
            return _login_page(next_path, f"Terlalu banyak percobaan. Coba lagi dalam {int(blocked) + 1} detik.", 429)
        # PBKDF2 sengaja lambat: jalankan di thread agar event loop tidak tertahan.
        if not await run_in_threadpool(site_gate.verify_password, form.get("password", ""), gate.password_hash or ""):
            site_gate.THROTTLE.register_failure(client)
            site_gate.GLOBAL_THROTTLE.register_failure(site_gate.GLOBAL_KEY)
            return _login_page(next_path, "Password salah.", 401)
        site_gate.THROTTLE.reset(client)
        response = RedirectResponse(next_path, status_code=303)
        secure = (
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").lower() == "https"
            or "https" in request.headers.get("cf-visitor", "")
        )
        response.set_cookie(
            site_gate.COOKIE_NAME,
            gate.issue(),
            max_age=gate.max_age,
            path="/",
            httponly=True,
            samesite="lax",
            secure=secure,
        )
        return response

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
                **_request_origin(request, context),
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
        # Konsol menyimpan kunci API di peramban: jangan bisa dibingkai situs lain (clickjacking),
        # jangan biarkan peramban menebak tipe berkas, jangan kirim alamat lengkap ke situs luar.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
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


def _clip(value: Any, limit: int = 200) -> Any:
    text = str(value or "").strip()
    return text[:limit] if text else None


_SENSITIVE_PARAM = re.compile(r"token|key|secret|pass|auth|code|sig|session|credential", re.I)


def _safe_query(query: str) -> str:
    """Query string untuk log, dengan nilai parameter yang mirip rahasia disamarkan."""
    pairs = parse_qsl(query or "", keep_blank_values=True)
    return urlencode([(k, "***" if _SENSITIVE_PARAM.search(k) else v) for k, v in pairs])


def _request_origin(request: Request, context: Any) -> Dict[str, Any]:
    """Dari mana permintaan datang: IP asli di balik Cloudflare, negara, situs web pemanggil
    (Origin/Referer), peramban/aplikasi (User-Agent), dan jenis kredensial yang dipakai.
    Hanya pengenal yang dicatat, tidak pernah nilai kunci atau token."""
    headers = request.headers
    session_id = getattr(context, "session_id", None)
    return {
        "client_ip": auth._client(request),
        "country": _clip(headers.get("CF-IPCountry"), 8),
        "host": _clip(headers.get("host"), 120),
        "query": _clip(_safe_query(request.url.query), 300),
        "origin": _clip(headers.get("origin"), 200),
        "referer": _clip(headers.get("referer"), 300),
        "user_agent": _clip(headers.get("user-agent"), 300),
        "credential": getattr(context, "source", None),
        "key_id": getattr(context, "key_id", None),
        "session": session_id[:8] if session_id else None,
    }


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
        # baris akses JSON dari middleware sudah lengkap (IP asli, origin, UA); log akses
        # bawaan uvicorn hanya menduplikasinya dengan IP proxy docker
        access_log=False,
    )


if __name__ == "__main__":
    run()
