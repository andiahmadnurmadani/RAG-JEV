"""Collection lifecycle + payload indexes (PRD 9)."""

from __future__ import annotations

from typing import Any, Dict, List

from qdrant_client import models as qmodels

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.qdrant.client import get_client

logger = get_logger(__name__)

PAYLOAD_INDEXES = {
    "organization_id": "keyword",
    "knowledge_base_id": "keyword",
    "document_id": "keyword",
    "chunk_id": "keyword",
    "language": "keyword",
}


def collection_exists(settings: Settings) -> bool:
    try:
        return get_client(settings).collection_exists(settings.qdrant_collection)
    except Exception:  # noqa: BLE001
        return False


def ensure_collection(settings: Settings, dim: int, *, recreate: bool = False) -> Dict[str, Any]:

    client = get_client(settings)
    name = settings.qdrant_collection

    if recreate and client.collection_exists(name):
        client.delete_collection(name)
        logger.info("dropped collection %s", name)

    created = False
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
        )
        created = True
        logger.info("created collection %s (dim=%d)", name, dim)

    for field, schema in PAYLOAD_INDEXES.items():
        try:
            client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=(
                    qmodels.PayloadSchemaType.KEYWORD if schema == "keyword" else qmodels.PayloadSchemaType.INTEGER
                ),
            )
        except Exception as exc:  # noqa: BLE001 — index already present is fine
            logger.debug("payload index for %s not created: %s", field, exc)

    return {"collection": name, "created": created, "dim": dim}


def collection_info(settings: Settings) -> Dict[str, Any]:
    client = get_client(settings)
    name = settings.qdrant_collection
    if not client.collection_exists(name):
        return {"collection": name, "exists": False, "points": 0, "dim": None}
    info = client.get_collection(name)
    vectors = info.config.params.vectors
    dim = getattr(vectors, "size", None)
    return {"collection": name, "exists": True, "points": info.points_count, "dim": dim}


def tenant_filter(organization_id: str, knowledge_base_id: str | None = None) -> Any:
    """The one and only tenant filter used by retrieval (PRD 13)."""

    if not organization_id:
        raise AppError("TENANT_CONTEXT_MISSING", "organization_id is required for every retrieval")
    conditions: List[Any] = [
        qmodels.FieldCondition(key="organization_id", match=qmodels.MatchValue(value=organization_id))
    ]
    if knowledge_base_id:
        conditions.append(
            qmodels.FieldCondition(key="knowledge_base_id", match=qmodels.MatchValue(value=knowledge_base_id))
        )
    return qmodels.Filter(must=conditions)


def delete_collection(settings: Settings) -> None:
    client = get_client(settings)
    if client.collection_exists(settings.qdrant_collection):
        client.delete_collection(settings.qdrant_collection)
