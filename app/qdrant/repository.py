"""Chunk persistence in Qdrant (PRD 9, 10, 13, 32)."""

from __future__ import annotations

import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from qdrant_client import models as qmodels

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.qdrant.client import get_client
from app.qdrant.collections import ensure_collection, tenant_filter

logger = get_logger(__name__)


def point_id(chunk_id: str, *, organization_id: str = "", knowledge_base_id: str = "", document_id: str = "") -> str:
    """Stable, collection-unique UUID for a chunk (Qdrant needs UUID or int ids).

    ``chunk_id`` is only unique *within a document* (PRD 9 shows ``chunk_001``), so
    the point id is derived from the full tenant + document path. Without this,
    ``chunk_0001`` of every document would collapse onto one point and each new
    document would silently overwrite the previous one.
    """
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rag-service:{organization_id}|{knowledge_base_id}|{document_id}|{chunk_id}",
        )
    )


def upsert_chunks(
    settings: Settings,
    *,
    dim: int,
    points: Sequence[Dict[str, Any]],
    batch_size: int = 128,
) -> int:
    """``points`` = [{"chunk_id", "vector", "payload"}, ...]."""

    if not points:
        return 0
    if dim <= 0:
        # An unresolved dimension (embedder not loaded yet) would create a collection of
        # size 0 that silently rejects every vector. Fail loudly instead.
        raise AppError("EMBEDDING_FAILED", "embedding dimension is unknown (embedder not loaded)")
    ensure_collection(settings, dim)
    client = get_client(settings)
    written = 0
    for start in range(0, len(points), batch_size):
        batch = points[start : start + batch_size]
        for item in batch:
            payload = item["payload"]
            if not payload.get("organization_id"):
                raise AppError("INDEXING_FAILED", "refusing to index a chunk without organization_id")
            if not payload.get("document_id"):
                raise AppError("INDEXING_FAILED", "refusing to index a chunk without document_id")
        client.upsert(
            collection_name=settings.qdrant_collection,
            points=[
                qmodels.PointStruct(
                    id=point_id(
                        item["payload"].get("chunk_id", ""),
                        organization_id=item["payload"].get("organization_id", ""),
                        knowledge_base_id=item["payload"].get("knowledge_base_id", ""),
                        document_id=item["payload"].get("document_id", ""),
                    ),
                    vector=item["vector"],
                    payload=item["payload"],
                )
                for item in batch
            ],
            wait=True,
        )
        written += len(batch)
    return written


def document_scope_condition(document_ids: Optional[Sequence[str]] = None):
    """``document_id IN (...)`` — always AND-ed onto the tenant filter, never instead of it.

    A caller may narrow *which* of its own documents to search, never *whose*: the
    organization filter is added by ``tenant_filter``/``get_chunks_by_ids`` independently.
    """
    ids = [str(item) for item in (document_ids or []) if item]
    if not ids:
        return None
    return qmodels.FieldCondition(key="document_id", match=qmodels.MatchAny(any=ids))


def search_dense(
    settings: Settings,
    *,
    vector: Sequence[float],
    organization_id: str,
    knowledge_base_id: Optional[str],
    top_k: int,
    extra_must: Optional[Sequence[Any]] = None,
    document_ids: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Tenant-filtered vector search. The filter is built here, never by callers."""

    if not collection_exists_guard(settings):
        return []
    query_filter = tenant_filter(organization_id, knowledge_base_id)
    must = list(query_filter.must or [])
    scope_condition = document_scope_condition(document_ids)
    if scope_condition is not None:
        must.append(scope_condition)
    if extra_must:
        must.extend(extra_must)
    if must != list(query_filter.must or []):
        query_filter = qmodels.Filter(must=must)
    client = get_client(settings)
    try:
        response = client.query_points(
            collection_name=settings.qdrant_collection,
            query=list(vector),
            query_filter=query_filter,
            limit=top_k,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise AppError("RETRIEVAL_FAILED", f"Qdrant search failed: {exc}") from exc
    hits = getattr(response, "points", response)
    results: List[Dict[str, Any]] = []
    for hit in hits:
        payload = dict(hit.payload or {})
        results.append(
            {
                "chunk_id": payload.get("chunk_id"),
                # document_id must be visible here: the fusion stage keys candidates by
                # (document_id, chunk_id), and without it every dense hit from every
                # document collapses onto the same "::chunk_0001" key.
                "document_id": payload.get("document_id"),
                "score": float(hit.score),
                "payload": payload,
            }
        )
    return results


def collection_exists_guard(settings: Settings) -> bool:
    try:
        return get_client(settings).collection_exists(settings.qdrant_collection)
    except Exception:  # noqa: BLE001
        return False


def delete_document(settings: Settings, *, organization_id: str, document_id: str) -> int:
    """Remove every chunk of one document inside one tenant (PRD 23, 32)."""

    if not collection_exists_guard(settings):
        return 0
    if not organization_id:
        raise AppError("TENANT_CONTEXT_MISSING", "organization_id is required for delete")
    query_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(key="organization_id", match=qmodels.MatchValue(value=organization_id)),
            qmodels.FieldCondition(key="document_id", match=qmodels.MatchValue(value=document_id)),
        ]
    )
    client = get_client(settings)
    before = count_document(settings, organization_id=organization_id, document_id=document_id)
    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=qmodels.FilterSelector(filter=query_filter),
        wait=True,
    )
    logger.info("deleted document %s for org %s (%d chunks)", document_id, organization_id, before)
    return before


def count_document(settings: Settings, *, organization_id: str, document_id: str) -> int:
    if not collection_exists_guard(settings):
        return 0
    client = get_client(settings)
    found = 0
    offset = None
    try:
        while True:
            records, offset = client.scroll(
                collection_name=settings.qdrant_collection,
                scroll_filter=_document_filter(organization_id, document_id),
                limit=256,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            found += len(records)
            if offset is None or not records:
                break
    except Exception as exc:  # noqa: BLE001 - penghitung pelengkap, bukan sumber kebenaran
        # Qdrant lokal (embedded) sesekali gagal menghitung setelah penghapusan dokumen
        # ("operands could not be broadcast"). Daftar dokumen tidak boleh ikut gagal 500
        # hanya karena kolom pelengkap ini; jumlah vektor dilaporkan tidak diketahui.
        logger.warning("gagal menghitung vektor dokumen %s: %s", document_id, exc)
        if found == 0:
            return -1
    return found


def _document_filter(organization_id: str, document_id: str):

    return qmodels.Filter(
        must=[
            qmodels.FieldCondition(key="organization_id", match=qmodels.MatchValue(value=organization_id)),
            qmodels.FieldCondition(key="document_id", match=qmodels.MatchValue(value=document_id)),
        ]
    )


def get_chunks_by_ids(
    settings: Settings,
    *,
    chunk_ids: Iterable[str],
    organization_id: str,
    knowledge_base_id: Optional[str] = None,
    document_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Fetch payloads for specific chunks, always inside the tenant boundary.

    Keys are ``"{document_id}::{chunk_id}"`` (see ``rag.retriever.fused_key``) because a
    bare ``chunk_0001`` repeats in every document — keying by it would silently drop one
    of two matching documents. Lookup is a tenant-filtered scroll, so a point outside the
    caller's organization can never be returned.
    """
    if not collection_exists_guard(settings):
        return {}

    keys = [key for key in chunk_ids if key]
    if not keys:
        return {}
    bare_chunk_ids = [key.partition("::")[2] or key for key in keys]

    conditions = [
        qmodels.FieldCondition(key="organization_id", match=qmodels.MatchValue(value=organization_id)),
        qmodels.FieldCondition(key="chunk_id", match=qmodels.MatchAny(any=bare_chunk_ids)),
    ]
    if knowledge_base_id:
        conditions.append(
            qmodels.FieldCondition(key="knowledge_base_id", match=qmodels.MatchValue(value=knowledge_base_id))
        )
    scope_condition = document_scope_condition(document_ids)
    if scope_condition is not None:
        conditions.append(scope_condition)
    query_filter = qmodels.Filter(must=conditions)

    client = get_client(settings)
    out: Dict[str, Dict[str, Any]] = {}
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=query_filter,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for record in records:
            payload = dict(record.payload or {})
            if payload.get("organization_id") != organization_id:
                # defence in depth: a foreign payload can never leave this function
                logger.error("tenant mismatch while retrieving chunk %s", payload.get("chunk_id"))
                continue
            key = f"{payload.get('document_id', '')}::{payload.get('chunk_id', '')}"
            if key in keys:
                out.setdefault(key, payload)
        if offset is None or not records:
            break
    return out


def list_document_chunks(
    settings: Settings,
    *,
    organization_id: str,
    document_id: str,
    knowledge_base_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Semua potongan satu dokumen, urut sesuai dokumen aslinya, di dalam batas tenant.

    Dipakai saat konteks perlu dilengkapi menjadi dokumen utuh: pencarian kemiripan selalu
    mengembalikan sebagian (potongan paling mirip), sedangkan pertanyaan "isi lengkap dokumen
    ini" hanya bisa dijawab benar kalau seluruh bagiannya ikut dikirim ke model. Payload di
    sini melewati filter tenant yang sama seperti pencarian - tidak ada jalan pintas.
    """
    if not collection_exists_guard(settings) or not document_id:
        return []

    conditions = [
        qmodels.FieldCondition(key="organization_id", match=qmodels.MatchValue(value=organization_id)),
        qmodels.FieldCondition(key="document_id", match=qmodels.MatchValue(value=document_id)),
    ]
    if knowledge_base_id:
        conditions.append(
            qmodels.FieldCondition(key="knowledge_base_id", match=qmodels.MatchValue(value=knowledge_base_id))
        )
    query_filter = qmodels.Filter(must=conditions)

    client = get_client(settings)
    out: List[Dict[str, Any]] = []
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=query_filter,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for record in records:
            payload = dict(record.payload or {})
            if payload.get("organization_id") != organization_id:
                logger.error("tenant mismatch while listing document %s", document_id)
                continue
            out.append(payload)
        if offset is None or not records:
            break

    def order(payload: Dict[str, Any]) -> Tuple[int, str]:
        index = payload.get("chunk_index")
        if isinstance(index, int):
            return index, str(payload.get("chunk_id", ""))
        raw = str(payload.get("chunk_id", ""))
        digits = raw.rsplit("_", 1)[-1]
        return (int(digits) if digits.isdigit() else 0), raw

    out.sort(key=order)
    return out


def tenant_point_count(settings: Settings, organization_id: str) -> int:
    if not collection_exists_guard(settings):
        return 0
    client = get_client(settings)
    response = client.count(
        collection_name=settings.qdrant_collection,
        count_filter=tenant_filter(organization_id, None),
        exact=True,
    )
    return int(getattr(response, "count", 0))
