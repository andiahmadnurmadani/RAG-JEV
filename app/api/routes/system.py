"""Health, readiness and metrics (PRD 27, 28, 36, 37)."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request, Response, status

from app.api.deps import services_from_request
from app.core.errors import ok
from app.core.metrics import snapshot
from app.parsing.formats import enabled_extensions
from app.parsing.web import describe_settings
from app.qdrant import client as qdrant_client
from app.qdrant import collections as qdrant_collections

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> Dict[str, Any]:
    """Liveness: the process is up. Never touches a dependency (PRD 27)."""
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request, response: Response) -> Dict[str, Any]:
    """Readiness: qdrant, embedding, reranker, llm, jev (PRD 28)."""
    services = services_from_request(request)
    settings = services.settings

    dependencies: Dict[str, str] = {
        "qdrant": qdrant_client.health(settings),
        "embedding": services.embedder.health(),
        "reranker": services.reranker.health(),
        "llm": services.generator.health(),
        "jev": services.jev.health(),
    }
    detail: Dict[str, Any] = {
        "collection": qdrant_collections.collection_info(settings),
        "embedding_provider": settings.embedding_provider,
        "embedding_model": services.embedder.name,
        "reranker_provider": settings.reranker_provider,
        "reranker_model": services.reranker.name,
        "llm_provider": settings.llm_provider,
        "llm_model": services.generator.model,
        "jev_mode": settings.jev_mode,
        "jev_provider": services.jev.provider,
        "jev_model": settings.jev_model or None,
        "jev_route_tool": settings.jev_route_tool,
        "worker": services.worker.stats(),
        "strict_grounding": settings.strict_grounding,
        "relevance_threshold": settings.relevance_threshold,
        # Published so a client can refuse an oversized upload *before* sending it.
        "max_upload_mb": settings.max_upload_mb,
        "allowed_mime": settings.allowed_mime_list,
        "allowed_extensions": enabled_extensions(settings),
        # Kebijakan sumber web, supaya klien tahu apa yang akan diterima server ini.
        "web": describe_settings(settings),
    }

    critical = ("qdrant", "embedding")
    if any(dependencies[name] != "ok" for name in critical):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ok({"status": "not_ready", "dependencies": dependencies, "detail": detail})

    # "disabled" is an explicit operator choice (optional dependency turned off), not a
    # failure: only a dependency that is on but broken makes the service degraded.
    problems = {
        name: value for name, value in dependencies.items() if value not in {"ok", "disabled"}
    }
    detail["disabled_dependencies"] = sorted(name for name, value in dependencies.items() if value == "disabled")
    return ok(
        {
            "status": "ready" if not problems else "degraded",
            "dependencies": dependencies,
            "detail": detail,
        }
    )


@router.get("/metrics")
def metrics(request: Request) -> Dict[str, Any]:
    """PRD 37 counters/latencies plus the honest state of the worker and indexes."""
    services = services_from_request(request)
    payload = snapshot()
    payload["worker"] = services.worker.stats()
    payload["sparse_scopes"] = services.sparse.stats()
    payload["collection"] = qdrant_collections.collection_info(services.settings)
    payload["jev_status"] = services.jev.status
    return payload
