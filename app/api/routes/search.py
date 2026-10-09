"""POST /search — retrieval without generation (PRD 25)."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

from app.api.deps import services_from_request
from app.api.middleware.auth import TrustedContext, trusted_context
from app.api.middleware.ratelimit import SlidingWindowLimiter
from app.api.middleware.tenant import guard_query_body, retrieval_scope
from app.api.schemas import SearchDataOut, SearchRequest, SearchResultOut
from app.core.errors import ok
from app.core.logging import get_logger
from app.rag.pipeline import ensure_knowledge_base

logger = get_logger(__name__)
router = APIRouter(tags=["search"])

_limiter: SlidingWindowLimiter | None = None


def _limit(request: Request, context: TrustedContext) -> None:
    global _limiter
    services = services_from_request(request)
    if _limiter is None:
        _limiter = SlidingWindowLimiter(services.settings.rate_limit_per_minute)
    _limiter.check(context.application_id)


@router.post("/search")
def search(
    payload: SearchRequest,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    _limit(request, context)
    context.require("read")
    guard_query_body(payload.model_dump(exclude_none=True), "/search")
    ensure_knowledge_base(payload.knowledge_base_id)
    scope = retrieval_scope(context, payload.knowledge_base_id)

    services = services_from_request(request)
    options = payload.options
    decision = services.rag.route(payload.query, context, options.route if options else None)

    retrieval = services.rag.search(
        query=payload.query,
        context=context,
        knowledge_base_id=scope.get("knowledge_base_id"),
        final_k=payload.top_k,
        use_hybrid=options.use_hybrid if options else None,
        use_reranker=options.use_reranker if options else None,
        threshold=options.threshold if options else None,
        document_ids=options.document_ids if options else None,
    )

    results = [
        SearchResultOut(
            document_id=candidate.document_id,
            chunk_id=candidate.chunk_id,
            content=candidate.content,
            score=round(float(candidate.score), 6),
            page=candidate.page,
            document_name=candidate.document_name,
            section=candidate.section,
            source_url=candidate.source_url,
            dense_score=_round(candidate.dense_score),
            sparse_score=_round(candidate.sparse_score),
            rerank_score=_round(candidate.rerank_score),
        )
        for candidate in retrieval.candidates
    ]
    if not results:
        logger.info("search returned no candidates org=%s kb=%s", context.organization_id, payload.knowledge_base_id)
    return ok(
        SearchDataOut(
            results=results,
            route=decision.to_dict(),
            hybrid=retrieval.hybrid_used,
            reranker=retrieval.reranker_used,
            retrieval_ms=retrieval.elapsed_ms,
            best_score=round(float(retrieval.best_score), 4),
            relevant=retrieval.relevant,
            relevance_gate=retrieval.gate,
            dense_weight=retrieval.dense_weight,
            dense_hits=retrieval.dense_hits,
            sparse_hits=retrieval.sparse_hits,
        ).model_dump()
    )


def _round(value):
    return None if value is None else round(float(value), 4)
