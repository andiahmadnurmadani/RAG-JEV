"""Vector-store write guards: a chunk is never stored with an unresolved dimension."""

from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.qdrant import repository


def _point(document_id: str = "doc_1", chunk_id: str = "chunk_0001"):
    return {
        "chunk_id": chunk_id,
        "vector": [0.1, 0.2, 0.3],
        "payload": {
            "organization_id": "org_a",
            "knowledge_base_id": "kb_a",
            "document_id": document_id,
            "chunk_id": chunk_id,
            "content": "isi",
        },
    }


def test_upsert_refuses_an_unknown_embedding_dimension(settings):
    """dim=0 means the embedder has not loaded: creating a size-0 collection would
    silently reject every vector later, so the write must fail now."""
    with pytest.raises(AppError) as excinfo:
        repository.upsert_chunks(settings, dim=0, points=[_point()])
    assert excinfo.value.code == "EMBEDDING_FAILED"


def test_upsert_refuses_a_chunk_without_a_tenant(settings):
    point = _point()
    point["payload"].pop("organization_id")
    with pytest.raises(AppError) as excinfo:
        repository.upsert_chunks(settings, dim=3, points=[point])
    assert excinfo.value.code == "INDEXING_FAILED"


def test_upsert_refuses_a_chunk_without_a_document(settings):
    point = _point()
    point["payload"].pop("document_id")
    with pytest.raises(AppError) as excinfo:
        repository.upsert_chunks(settings, dim=3, points=[point])
    assert excinfo.value.code == "INDEXING_FAILED"


def test_point_ids_are_unique_per_document_for_the_same_chunk_id():
    """chunk_0001 exists in every document; the two must not share a point id."""
    first = repository.point_id(
        "chunk_0001", organization_id="org_a", knowledge_base_id="kb_a", document_id="doc_1"
    )
    second = repository.point_id(
        "chunk_0001", organization_id="org_a", knowledge_base_id="kb_a", document_id="doc_2"
    )
    assert first != second
