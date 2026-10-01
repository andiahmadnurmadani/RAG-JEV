"""POST /extract — structured data out of knowledge (PRD 26, 30)."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

from app.api.deps import services_from_request
from app.api.middleware.auth import TrustedContext, trusted_context
from app.api.middleware.ratelimit import SlidingWindowLimiter
from app.api.middleware.tenant import guard_query_body, retrieval_scope
from app.api.schemas import ExtractDataOut, ExtractRequest, SourceOut
from app.core.errors import ok
from app.core.logging import get_logger
from app.rag.pipeline import ensure_knowledge_base

logger = get_logger(__name__)
router = APIRouter(tags=["extract"])

_limiter: SlidingWindowLimiter | None = None


def _limit(request: Request, context: TrustedContext) -> None:
    global _limiter
    services = services_from_request(request)
    if _limiter is None:
        _limiter = SlidingWindowLimiter(services.settings.rate_limit_per_minute)
    _limiter.check(context.application_id)


@router.post("/extract")
def extract(
    payload: ExtractRequest,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    _limit(request, context)
    context.require("read")
    guard_query_body(payload.model_dump(exclude_none=True), "/extract")
    ensure_knowledge_base(payload.knowledge_base_id)
    scope = retrieval_scope(context, payload.knowledge_base_id)

    services = services_from_request(request)
    result = services.rag.extract(
        query=payload.query,
        context=context,
        knowledge_base_id=scope.get("knowledge_base_id"),
        output_schema=payload.output_schema,
        top_k=payload.top_k,
        document_ids=payload.document_ids,
    )

    data = ExtractDataOut(
        items=result.extracted,
        sources=[SourceOut(**source) for source in result.sources],
        not_found=not result.grounded,
        route=result.route.to_dict() if result.route else None,
    )
    logger.info(
        "extract served org=%s items=%d not_found=%s",
        context.organization_id,
        len(result.extracted),
        data.not_found,
    )
    return ok(data.model_dump())
