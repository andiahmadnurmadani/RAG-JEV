"""Ganti model embedding dari Pengaturan: knowledge lama di-embed ulang tanpa unggah ulang."""

from __future__ import annotations

import time

from tests.conftest import TENANT_A_KEY, auth, wait_for_job

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def test_ganti_embedding_migrasi_otomatis(client, settings, monkeypatch):
    import app.rag.embedder as embedder_module

    # Model sungguhan (ratusan MB) diganti embedder ringan: yang diuji alurnya, bukan modelnya.
    class FakeSemantic(embedder_module.HashEmbedder):
        name = "fake-semantic"

    original = embedder_module.build_embedder
    monkeypatch.setattr(
        embedder_module,
        "build_embedder",
        lambda s: FakeSemantic() if s.embedding_provider == "fastembed" else original(s),
    )

    for number in range(3):
        response = client.post(
            "/api/v1/knowledge/index",
            json={"document_id": f"doc_{number}", "knowledge_base_id": "kb_emb", "document_name": f"SOP {number}.md",
                  "text": f"# SOP {number}\nJatah cuti tahunan pegawai nomor {number} adalah 12 hari kerja."},
            headers=auth(TENANT_A_KEY),
        )
        assert response.status_code == 202
        wait_for_job(client, f"doc_{number}")
    old_collection = settings.qdrant_collection

    bad = client.put("/api/v1/settings", json={"embedding": {"provider": "fastembed", "model": "model/ngawur"}},
                     headers=auth(TENANT_A_KEY))
    assert bad.status_code == 422

    switched = client.put("/api/v1/settings", json={"embedding": {"provider": "fastembed", "model": MODEL}},
                          headers=auth(TENANT_A_KEY))
    assert switched.status_code == 200, switched.text
    status = switched.json()["data"]["embedding_status"]
    assert status["collection"] != old_collection and status["collection"].startswith(old_collection + "__")

    deadline = time.time() + 30
    while time.time() < deadline:
        status = client.get("/api/v1/settings", headers=auth(TENANT_A_KEY)).json()["data"]["embedding_status"]
        if status.get("state") == "done":
            break
        time.sleep(0.2)
    assert status["state"] == "done", status
    assert status["done"] == status["total"] >= 3

    hits = client.post("/api/v1/search", json={"query": "jatah cuti tahunan", "knowledge_base_id": "kb_emb"},
                       headers=auth(TENANT_A_KEY)).json()["data"]["results"]
    assert hits, "knowledge lama harus tetap bisa dicari setelah migrasi"

    # kembali ke hash: koleksi lama dipakai lagi (tidak dihapus)
    back = client.put("/api/v1/settings", json={"embedding": {"provider": "hash"}}, headers=auth(TENANT_A_KEY))
    assert back.json()["data"]["embedding_status"]["collection"] == old_collection


def test_migrasi_gagal_dilanjutkan_bukan_dianggap_selesai(client, settings):
    """Migrasi yang gagal/terputus tidak boleh berstatus 'done' setelah restart, dan migrasi
    yang belum selesai tidak pernah dijadikan sumber migrasi berikutnya."""
    from app.rag.embedding_migration import collection_for

    migrator = client.app.state.services.migrator
    base = migrator.base_collection
    target = collection_for(base, "fastembed", MODEL)
    migrator._save(state="failed", source=base, target=target, active=target, done=1, total=3)
    settings.embedding_provider, settings.embedding_fastembed_model = "fastembed", MODEL

    response = client.post("/api/v1/knowledge/index", json={"document_id": "doc_x", "knowledge_base_id": "kb_emb",
                                                            "document_name": "x.md", "text": "Kuota cuti 12 hari."},
                           headers=auth(TENANT_A_KEY))
    assert response.status_code == 202
    wait_for_job(client, "doc_x")
    settings.qdrant_collection = base
    migrator.activate(start=False)
    status = migrator.status()
    assert status["state"] == "running" and status["source"] == base
    assert settings.qdrant_migration_source == base

    # model diganti lagi sebelum selesai: sumbernya tetap koleksi lengkap, bukan target parsial
    settings.embedding_fastembed_model = "intfloat/multilingual-e5-large"
    migrator.activate(start=False)
    assert migrator.status()["source"] == base


def test_hapus_selama_migrasi_ikut_menghapus_di_sumber(client, settings):
    from app.qdrant import repository
    from app.qdrant.client import get_client

    response = client.post("/api/v1/knowledge/index", json={"document_id": "doc_hapus", "knowledge_base_id": "kb_emb",
                                                            "document_name": "h.md", "text": "Kuota cuti 12 hari."},
                           headers=auth(TENANT_A_KEY))
    assert response.status_code == 202
    wait_for_job(client, "doc_hapus")
    source = settings.qdrant_collection
    assert repository.count_document(settings, organization_id="org_a", document_id="doc_hapus") > 0
    settings.qdrant_collection = source + "__lain"
    settings.qdrant_migration_source = source
    repository.delete_document(settings, organization_id="org_a", document_id="doc_hapus")
    settings.qdrant_collection = source
    settings.qdrant_migration_source = ""
    assert repository.count_document(settings, organization_id="org_a", document_id="doc_hapus") == 0
    assert get_client(settings).collection_exists(source)
