"""Knowledge indexing, status and deletion (PRD 22, 23, 32, 33, 45)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import services_from_request
from app.api.middleware.auth import TrustedContext, trusted_context
from app.api.middleware.ratelimit import SlidingWindowLimiter
from app.api.middleware.tenant import assert_tenant_match
from app.api.schemas import DocumentStatusOut, IndexDataOut, KnowledgeIndexRequest, KnowledgeUpdateRequest
from app.core.errors import AppError, ok
from app.core.logging import get_logger
from app.core.security import validate_display_label, validate_payload_size
from app.core.urlguard import UrlRejected, assert_public_url
from app.qdrant import repository
from app.workers.indexing import STATUS_DELETED

logger = get_logger(__name__)
router = APIRouter(tags=["knowledge"])

_limiter: SlidingWindowLimiter | None = None


def _rate_limit(request: Request) -> None:
    global _limiter
    services = services_from_request(request)
    if _limiter is None:
        _limiter = SlidingWindowLimiter(services.settings.rate_limit_per_minute)
    identity = getattr(request.state, "tenant_context", None)
    key = f"{(identity.application_id if identity else 'anon')}"
    _limiter.check(key)


def validate_source_urls(payload, settings) -> None:
    """Tolak URL yang bukan alamat publik SEBELUM pekerjaan dibuat.

    Pemeriksaan ini diulang di worker (dan di setiap pengalihan), tetapi menolak lebih awal
    membuat kesalahannya jelas bagi pemanggil: 422, bukan pekerjaan yang lalu gagal diam-diam.
    """
    allow_private = bool(getattr(settings, "allow_private_urls", False))
    for label, value in (("file_url", getattr(payload, "file_url", "")), ("web_url", getattr(payload, "web_url", ""))):
        if not value:
            continue
        try:
            assert_public_url(value, allow_private=allow_private)
        except UrlRejected as exc:
            raise AppError(
                "VALIDATION_ERROR",
                f"{label} ditolak: {exc}",
                details={label: value[:200]},
            ) from exc
    if getattr(payload, "web_url", "") and not getattr(settings, "web_crawl_enabled", True):
        raise AppError("VALIDATION_ERROR", "crawl web sedang dimatikan di setelan layanan (WEB_CRAWL_ENABLED=false)")


@router.post("/knowledge/index", status_code=status.HTTP_202_ACCEPTED)
def index_knowledge(
    payload: KnowledgeIndexRequest,
    request: Request,
    response: Response,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Enqueue an indexing job (PRD 33: 202 Accepted, work happens in the worker)."""
    _rate_limit(request)
    context.require("write")
    context.require_knowledge_base(payload.knowledge_base_id)
    organization_id = assert_tenant_match(context, payload.organization_id, where="/knowledge/index")

    # Pre-flight: a label that claims a type we never accept (payload.exe, script.js, ...) is
    # rejected before a job is queued or a byte is read. The worker keeps the same check as a
    # backstop for sources whose real name only appears after download (file_url).
    services = services_from_request(request)
    validate_display_label(payload.document_name or "", services.settings)
    validate_payload_size(services.settings, payload.content_base64, payload.text)
    validate_source_urls(payload, services.settings)
    job = services.worker.submit(
        {
            "document_id": payload.document_id,
            "organization_id": organization_id,
            "knowledge_base_id": payload.knowledge_base_id,
            "document_name": payload.document_name or "",
            "file_url": payload.file_url or "",
            "web_url": payload.web_url or "",
            "web_max_pages": payload.web_max_pages,
            "web_max_depth": payload.web_max_depth,
            "web_follow_files": payload.web_follow_files,
            "content_base64": payload.content_base64 or "",
            "text": payload.text or "",
            "metadata": payload.metadata,
            "language": payload.language or "",
            "replace": payload.replace,
        }
    )
    response.headers["X-Job-Id"] = job.job_id
    logger.info(
        "index requested document=%s org=%s kb=%s job=%s",
        payload.document_id,
        organization_id,
        payload.knowledge_base_id,
        job.job_id,
    )
    return ok(IndexDataOut(document_id=payload.document_id, status=job.status, job_id=job.job_id).model_dump())


@router.get("/knowledge")
def list_knowledge(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
    knowledge_base_id: Optional[str] = Query(default=None, max_length=200),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
) -> Dict[str, Any]:
    """List this organization's documents (newest first).

    There is no ``organization_id`` parameter on purpose: a listing endpoint that accepted
    one would be the easiest place in the service to leak another tenant's catalogue.
    """
    _rate_limit(request)
    context.require("read")
    services = services_from_request(request)

    if knowledge_base_id:
        context.require_knowledge_base(knowledge_base_id)
    records: List[Any] = services.jobs.list_documents(
        context.organization_id,
        knowledge_base_id=knowledge_base_id,
        include_deleted=include_deleted,
        limit=limit if not context.knowledge_base_ids else 500,
    )
    if context.knowledge_base_ids:
        records = [record for record in records if context.allows_knowledge_base(record.knowledge_base_id)][:limit]
    documents = [
        DocumentStatusOut(
            document_id=record.document_id,
            document_name=record.document_name,
            status=record.status,
            stage=record.stage,
            knowledge_base_id=record.knowledge_base_id,
            chunks=record.chunks,
            tokens=record.tokens,
            pages=record.pages,
            error=record.error,
            created_at=record.created_at,
            updated_at=record.updated_at,
            duration_ms=record.duration_ms,
            vectors_in_store=repository.count_document(
                services.settings,
                organization_id=context.organization_id,
                document_id=record.document_id,
            ),
            tables=record.tables,
            source_url=record.source_url or "",
            summary=record.summary or "",
            summary_tokens=record.summary_tokens,
            summary_error=record.summary_error or "",
        ).model_dump()
        for record in records
    ]
    if knowledge_base_id and not include_deleted:
        # Dokumen lama yang catatan pengindeksannya sudah tidak ada, tetapi isinya masih bisa
        # dicari: tetap ditampilkan supaya isi knowledge base terlihat utuh.
        known = {item["document_id"] for item in documents}
        for document_id, chunks in services.sparse.document_chunk_counts(
            context.organization_id, knowledge_base_id
        ).items():
            if document_id in known or len(documents) >= limit:
                continue
            documents.append(
                DocumentStatusOut(
                    document_id=document_id,
                    document_name=document_id,
                    status="completed",
                    stage="completed",
                    knowledge_base_id=knowledge_base_id,
                    chunks=chunks,
                    vectors_in_store=chunks,
                ).model_dump()
            )
    logger.info(
        "knowledge listed org=%s kb=%s documents=%d",
        context.organization_id,
        knowledge_base_id or "*",
        len(documents),
    )
    return ok({"documents": documents, "count": len(documents), "knowledge_base_id": knowledge_base_id})


@router.get("/knowledge-bases")
def list_knowledge_bases(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Semua knowledge base organisasi pemanggil, dengan jumlah dokumen/potongan dan status.

    Knowledge base tidak dibuat terpisah - ia muncul begitu dokumen pertamanya diindeks. Daftar
    ini menggabungkan catatan pengindeksan (status, waktu) dengan indeks kata kunci (jumlah
    potongan yang benar-benar bisa dicari), sehingga KB lama yang catatan job-nya sudah tidak
    lengkap tetap terlihat. Kunci yang diikat ke KB tertentu hanya melihat KB miliknya.
    """
    _rate_limit(request)
    context.require("read")
    services = services_from_request(request)
    bases: Dict[str, Dict[str, Any]] = {}

    def entry(knowledge_base_id: str) -> Dict[str, Any]:
        return bases.setdefault(
            knowledge_base_id,
            {
                "knowledge_base_id": knowledge_base_id,
                "documents": 0,
                "chunks": 0,
                "tokens": 0,
                "completed": 0,
                "processing": 0,
                "failed": 0,
                "updated_at": "",
            },
        )

    for record in services.jobs.list_documents(context.organization_id, limit=100_000):
        item = entry(record.knowledge_base_id or "")
        item["documents"] += 1
        item["chunks"] += int(record.chunks or 0)
        item["tokens"] += int(record.tokens or 0)
        bucket = record.status if record.status in ("completed", "failed") else "processing"
        item[bucket] += 1
        item["updated_at"] = max(item["updated_at"], record.updated_at or record.created_at or "")

    for knowledge_base_id, counts in services.sparse.knowledge_bases(context.organization_id).items():
        item = entry(knowledge_base_id)
        # Indeks kata kunci = yang benar-benar bisa dicari; catatan job bisa sudah terpangkas.
        item["documents"] = max(item["documents"], counts["documents"])
        item["chunks"] = counts["chunks"] or item["chunks"]
        if not item["completed"] and not item["processing"] and not item["failed"]:
            item["completed"] = counts["documents"]

    items = [
        item for key, item in bases.items()
        if key and context.allows_knowledge_base(key) and (item["documents"] or item["processing"])
    ]
    items.sort(key=lambda item: (item["updated_at"] or "", item["knowledge_base_id"]), reverse=True)
    return ok({"knowledge_bases": items, "count": len(items)})


@router.put("/knowledge/{document_id}", status_code=status.HTTP_202_ACCEPTED)
def update_knowledge(
    document_id: str,
    payload: KnowledgeUpdateRequest,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Re-index a document: old vectors are removed before the new ones land (PRD 32)."""
    _rate_limit(request)
    context.require("write")
    if payload.document_id != document_id:
        raise AppError("VALIDATION_ERROR", "document_id in the path and body must match")
    context.require_knowledge_base(payload.knowledge_base_id)
    organization_id = assert_tenant_match(context, payload.organization_id, where="/knowledge/{id}")

    services = services_from_request(request)
    validate_source_urls(payload, services.settings)
    job = services.worker.submit(
        {
            "document_id": document_id,
            "organization_id": organization_id,
            "knowledge_base_id": payload.knowledge_base_id,
            "document_name": payload.document_name or "",
            "file_url": payload.file_url or "",
            "web_url": payload.web_url or "",
            "web_max_pages": payload.web_max_pages,
            "web_max_depth": payload.web_max_depth,
            "web_follow_files": payload.web_follow_files,
            "content_base64": payload.content_base64 or "",
            "text": payload.text or "",
            "metadata": payload.metadata,
            "language": payload.language or "",
            "replace": True,
        }
    )
    return ok(IndexDataOut(document_id=document_id, status=job.status, job_id=job.job_id).model_dump())


@router.delete("/knowledge/{document_id}")
def delete_knowledge(
    document_id: str,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Delete every chunk/vector of a document inside the caller's organization."""
    _rate_limit(request)
    context.require("write")
    services = services_from_request(request)

    record = services.jobs.by_document(context.organization_id, document_id)
    bound_scope = None
    if context.knowledge_base_ids:
        # Kunci proyek: dokumen di KB lain (walau organisasinya sama) diperlakukan tidak ada, dan
        # penghapusan dibatasi ke KB dokumen itu - dokumen ber-ID sama di proyek lain aman.
        if record is None or not context.allows_knowledge_base(record.knowledge_base_id):
            raise AppError("DOCUMENT_NOT_FOUND", f"document '{document_id}' was not found in this organization")
        bound_scope = record.knowledge_base_id
    removed_vectors = repository.delete_document(
        services.settings,
        organization_id=context.organization_id,
        document_id=document_id,
        knowledge_base_id=bound_scope,
    )
    removed_lexical = 0
    if record is not None:
        removed_lexical = services.sparse.remove_document(
            context.organization_id, record.knowledge_base_id, document_id
        )
    removed_tables = services.tables.delete_document(
        organization_id=context.organization_id, document_id=document_id
    )
    services.jobs.mark_deleted(context.organization_id, document_id)

    if removed_vectors == 0 and record is None:
        raise AppError("DOCUMENT_NOT_FOUND", f"document '{document_id}' was not found in this organization")

    logger.info(
        "deleted document %s org %s (vectors=%d lexical=%d tables=%d)",
        document_id,
        context.organization_id,
        removed_vectors,
        removed_lexical,
        removed_tables,
    )
    return ok({"document_id": document_id, "status": STATUS_DELETED, "deleted_chunks": removed_vectors,
 "deleted_tables": removed_tables,
 })


@router.get("/knowledge/{document_id}")
def knowledge_status(
    document_id: str,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Job status for one document, scoped to the caller's organization."""
    _rate_limit(request)
    context.require("read")
    services = services_from_request(request)
    record = services.jobs.by_document(context.organization_id, document_id)
    if record is None or not context.allows_knowledge_base(record.knowledge_base_id):
        raise AppError("DOCUMENT_NOT_FOUND", f"document '{document_id}' is unknown to this organization")
    vectors = repository.count_document(
        services.settings, organization_id=context.organization_id, document_id=document_id
    )
    payload = DocumentStatusOut(
        document_id=record.document_id,
        document_name=record.document_name,
        status=record.status,
        stage=record.stage,
        knowledge_base_id=record.knowledge_base_id,
        chunks=record.chunks,
        tokens=record.tokens,
        pages=record.pages,
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        duration_ms=record.duration_ms,
        vectors_in_store=vectors,
        tables=record.tables,
        source_url=record.source_url or "",
        summary=record.summary or "",
        summary_tokens=record.summary_tokens,
        summary_error=record.summary_error or "",
    )
    return ok(payload.model_dump())
