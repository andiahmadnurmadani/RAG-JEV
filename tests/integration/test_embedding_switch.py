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
