"""Hitungan vektor per dokumen dibaca dari satu snapshot koleksi, bukan satu pemindaian per KB.

Integrasi KMS mem-poll daftar knowledge untuk ratusan KB tiap 45 detik; di Qdrant tertanam setiap
scroll berfilter memindai seluruh koleksi, jadi tiap poll dulu = ratusan pemindaian penuh.
"""

from __future__ import annotations

from app.qdrant import repository
from app.qdrant.client import get_client


def _point(org: str, kb: str, document_id: str, chunk_id: str):
    return {
        "chunk_id": chunk_id,
        "vector": [0.1, 0.2, 0.3],
        "payload": {"organization_id": org, "knowledge_base_id": kb, "document_id": document_id, "chunk_id": chunk_id, "content": "isi"},
    }


def test_many_knowledge_bases_share_one_scan_and_writes_show_up_at_once(settings, monkeypatch):
    repository.invalidate_document_counts()
    repository.upsert_chunks(
        settings,
        dim=3,
        points=[
            _point("org_a", "kb_1", "doc_1", "c1"),
            _point("org_a", "kb_1", "doc_1", "c2"),
            _point("org_a", "kb_2", "doc_2", "c1"),
            _point("org_b", "kb_1", "doc_9", "c1"),
        ],
    )
    client = get_client(settings)
    scans = []
    real_scroll = client.scroll
    monkeypatch.setattr(client, "scroll", lambda *a, **kw: scans.append(1) or real_scroll(*a, **kw))

    assert repository.count_by_document(settings, organization_id="org_a", knowledge_base_id="kb_1") == {"doc_1": 2}
    assert repository.count_by_document(settings, organization_id="org_a", knowledge_base_id="kb_2") == {"doc_2": 1}
    assert repository.count_by_document(settings, organization_id="org_a") == {"doc_1": 2, "doc_2": 1}
    assert repository.count_by_document(settings, organization_id="org_a", knowledge_base_id="kb_kosong") == {}
    # tenant lain tidak ikut terhitung
    assert repository.count_by_document(settings, organization_id="org_b", knowledge_base_id="kb_1") == {"doc_9": 1}
    assert len(scans) == 1

    repository.upsert_chunks(settings, dim=3, points=[_point("org_a", "kb_1", "doc_3", "c1")])
    assert repository.count_by_document(settings, organization_id="org_a", knowledge_base_id="kb_1") == {"doc_1": 2, "doc_3": 1}
    repository.delete_document(settings, organization_id="org_a", document_id="doc_1")
    assert repository.count_by_document(settings, organization_id="org_a", knowledge_base_id="kb_1") == {"doc_3": 1}
    repository.delete_knowledge_base(settings, organization_id="org_a", knowledge_base_id="kb_2")
    assert repository.count_by_document(settings, organization_id="org_a", knowledge_base_id="kb_2") == {}
