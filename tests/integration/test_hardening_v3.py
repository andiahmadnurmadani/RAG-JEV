"""Temuan audit production (putaran 3): akses, hapus saat antre, migrasi embedding, jawaban."""

from __future__ import annotations

import base64

from tests.conftest import READ_ONLY_KEY, TENANT_A_KEY, TENANT_B_KEY, auth, wait_for_job

KEYS = "/api/v1/settings/api-keys"


def _bound_key(client, kb: str) -> str:
    created = client.post(KEYS, json={"label": "proyek", "permissions": ["read", "write"], "knowledge_base_ids": [kb]},
                          headers=auth(TENANT_A_KEY))
    assert created.status_code == 200, created.text
    return created.json()["data"]["key"]


def _index(client, document_id: str, kb: str, key: str = TENANT_A_KEY, text: str = "Kuota cuti tahunan 12 hari kerja."):
    return client.post("/api/v1/knowledge/index", json={"document_id": document_id, "knowledge_base_id": kb,
                                                        "document_name": f"{document_id}.md", "text": text},
                       headers=auth(key))


def test_kunci_terikat_kb_bukan_admin_dan_tidak_melihat_tabel_kb_lain(client):
    key = _bound_key(client, "kb_a")
    assert client.get("/api/v1/settings", headers=auth(key)).status_code == 403
    assert client.get(KEYS, headers=auth(key)).status_code == 403
    assert client.get("/api/v1/tables?knowledge_base_id=kb_b", headers=auth(key)).status_code == 403
    assert client.get("/api/v1/tables", headers=auth(key)).status_code == 403
    assert client.get("/api/v1/tables?knowledge_base_id=kb_a", headers=auth(key)).status_code == 200


def test_tenant_lain_dan_kunci_baca_tidak_mengubah_setelan_global(client):
    for key in (TENANT_B_KEY, READ_ONLY_KEY):
        response = client.put("/api/v1/settings", json={"llm": {"model": "x/y"}}, headers=auth(key))
        assert response.status_code == 403, key


def test_metrics_butuh_kredensial_operator(client):
    assert client.get("/api/v1/metrics").status_code == 401
    assert client.get("/api/v1/metrics", headers=auth(TENANT_B_KEY)).status_code == 403
    assert client.get("/api/v1/metrics", headers=auth(TENANT_A_KEY)).status_code == 200


def test_gerbang_publik_tidak_membocorkan_potongan_kode(client):
    client.put("/api/v1/settings/access", json={"code": "rahasia-123456"}, headers=auth(TENANT_A_KEY))
    data = client.get("/api/v1/auth/gate").json()["data"]
    assert data["code_set"] is True
    assert "hint" not in data and "active_sessions" not in data


def test_document_id_kb_lain_tidak_bisa_ditimpa(client):
    assert _index(client, "doc_sama", "kb_a").status_code == 202
    wait_for_job(client, "doc_sama")
    clash = _index(client, "doc_sama", "kb_b", text="isi lain")
    assert clash.status_code == 422
    # dokumen di kb_a tetap utuh dan tetap milik kb_a
    hits = client.post("/api/v1/search", json={"query": "kuota cuti tahunan", "knowledge_base_id": "kb_a"},
                       headers=auth(TENANT_A_KEY)).json()["data"]["results"]
    assert any(hit["document_id"] == "doc_sama" for hit in hits)
    # mengindeks ulang di KB yang sama tetap boleh
    assert _index(client, "doc_sama", "kb_a", text="Kuota cuti tahunan 14 hari kerja.").status_code == 202


def test_dokumen_yang_dihapus_saat_antre_tidak_hidup_lagi(client, settings):
    services = client.app.state.services
    store = services.jobs
    record = store.create(document_id="doc_antre", organization_id="org_a", knowledge_base_id="kb_a",
                          document_name="antre.md")
    store.mark_deleted("org_a", "doc_antre")
    result = services.worker._pipeline.run(job_id=record.job_id, document_id="doc_antre", organization_id="org_a",
                                           knowledge_base_id="kb_a", document_name="antre.md",
                                           text="Kuota cuti tahunan 12 hari kerja.")
    assert result.status == "deleted"
    assert store.get(record.job_id).status == "deleted"
    hits = client.post("/api/v1/search", json={"query": "kuota cuti", "knowledge_base_id": "kb_a"},
                       headers=auth(TENANT_A_KEY)).json()["data"]["results"]
    assert not any(hit["document_id"] == "doc_antre" for hit in hits)


def test_tabel_ikut_dibuang_bila_dokumen_dihapus_saat_diindeks(client):
    services = client.app.state.services
    if services.tables is None:
        return
    csv = base64.b64encode(b"bulan,penjualan\nJanuari,10\nFebruari,20\n").decode()
    store = services.jobs
    record = store.create(document_id="doc_tabel", organization_id="org_a", knowledge_base_id="kb_a",
                          document_name="jual.csv")
    original = services.worker._pipeline._encode

    def encode_then_delete(*args, **kwargs):
        store.mark_deleted("org_a", "doc_tabel")
        return original(*args, **kwargs)

    services.worker._pipeline._encode = encode_then_delete
    try:
        services.worker._pipeline.run(job_id=record.job_id, document_id="doc_tabel", organization_id="org_a",
                                      knowledge_base_id="kb_a", document_name="jual.csv", content_base64=csv)
    finally:
        services.worker._pipeline._encode = original
    tables = client.get("/api/v1/tables?knowledge_base_id=kb_a", headers=auth(TENANT_A_KEY)).json()["data"]["tables"]
    assert not any(table["document_id"] == "doc_tabel" for table in tables)


def test_setelan_ngawur_ditolak_bukan_merusak(client):
    junk = client.put("/api/v1/settings", json={"unanswered": {"retention_days": "abc"}}, headers=auth(TENANT_A_KEY))
    assert junk.status_code == 422
    empty = client.put("/api/v1/settings", json={"embedding": {"model": ""}}, headers=auth(TENANT_A_KEY))
    assert empty.status_code in (200, 422)
    status = client.get("/api/v1/settings", headers=auth(TENANT_A_KEY)).json()["data"]["embedding_status"]
    assert not status["collection"].endswith("__model"), "model kosong tidak boleh tersimpan"
