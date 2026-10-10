"""POST /query — grounded answer with citations (PRD 12, 16, 24)."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

from app.api.deps import services_from_request
from app.api.middleware.auth import TrustedContext, trusted_context
from app.api.middleware.ratelimit import SlidingWindowLimiter
from app.api.middleware.tenant import guard_query_body, retrieval_scope
from app.api.schemas import ComputedOut, QueryDataOut, QueryRequest, SourceOut
from app.core.errors import ok
from app.core.logging import get_logger
from app.rag.pipeline import ensure_knowledge_base

logger = get_logger(__name__)
router = APIRouter(tags=["query"])

_limiter: SlidingWindowLimiter | None = None


def _limit(request: Request, context: TrustedContext) -> None:
    global _limiter
    services = services_from_request(request)
    if _limiter is None:
        _limiter = SlidingWindowLimiter(services.settings.rate_limit_per_minute)
    _limiter.check(context.application_id)


@router.post("/query")
def query(
    payload: QueryRequest,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    _limit(request, context)
    context.require("read")
    # PRD 6/21: the client may not choose its tenant.
    guard_query_body(payload.model_dump(exclude_none=True), "/query")
    ensure_knowledge_base(payload.knowledge_base_id)
    scope = retrieval_scope(context, payload.knowledge_base_id)

    services = services_from_request(request)
    options = payload.options
    result = services.rag.answer(
        query=payload.query,
        context=context,
        knowledge_base_id=scope.get("knowledge_base_id"),
        top_k=options.top_k,
        strict_grounding=options.strict_grounding,
        include_sources=options.include_sources,
        use_hybrid=options.use_hybrid,
        use_reranker=options.use_reranker,
        threshold=options.threshold,
        route_hint=options.route,
        document_ids=options.document_ids,
        table_analytics=options.table_analytics,
        history=[turn.model_dump() for turn in payload.history or []],
    )

    _record_unanswered(services, context, scope.get("knowledge_base_id") or "", payload.query, result)

    data = QueryDataOut(
        answer=result.answer,
        grounded=result.grounded,
        sources=[SourceOut(**source) for source in result.sources],
        usage=result.usage or {},
        route=result.route.to_dict() if result.route else None,
        model=result.model,
        no_answer_reason=result.no_answer_reason,
        computed=ComputedOut(**result.computed) if result.computed else None,
        table_note=result.table_note,
    )
    logger.info(
        "query served org=%s app=%s grounded=%s sources=%d rows=%s reason=%s",
        context.organization_id,
        context.application_id,
        result.grounded,
        len(result.sources),
        (result.computed or {}).get("rows_matched"),
        result.no_answer_reason,
    )
    return ok(data.model_dump())

def _record_unanswered(services, context: TrustedContext, knowledge_base_id: str, query_text: str, result) -> None:
    """Catat pertanyaan yang berakhir "tidak ditemukan" - bahan operator melengkapi knowledge.

    Kegagalan mencatat tidak boleh menggagalkan jawaban.
    """
    settings = services.settings
    if result.grounded or not settings.unanswered_enabled:
        return
    wanted = {item.strip() for item in str(settings.unanswered_reasons or "").split(",") if item.strip()}
    reason = result.no_answer_reason or ""
    if reason not in wanted:
        return
    try:
        services.unanswered.record(
            organization_id=context.organization_id,
            knowledge_base_id=knowledge_base_id,
            query=query_text,
            reason=reason,
            best_score=(result.usage or {}).get("best_score"),
            application_id=context.application_id,
            retention_days=int(settings.unanswered_retention_days or 0),
            max_entries=int(settings.unanswered_max_entries or 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("gagal mencatat pertanyaan tak terjawab: %s", exc)


@router.get("/tables")
def list_tables(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
    knowledge_base_id: str | None = None,
) -> Dict[str, Any]:
    """Tabel terstruktur yang tersedia di scope pemanggil (untuk perhitungan agregat)."""

    _limit(request, context)
    context.require("read")
    services = services_from_request(request)
    tables = services.tables.list_tables(
        organization_id=context.organization_id, knowledge_base_id=knowledge_base_id
    )
    return ok(
        {
            "count": len(tables),
            "tables": [table.as_dict() for table in tables],
            "analytics_enabled": services.settings.table_analytics_enabled,
        }
    )

