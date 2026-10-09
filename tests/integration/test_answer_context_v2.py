"""Konteks jawaban: tetangga di sekitar hasil (bukan awal dokumen), sitasi, riwayat, kunci per KB."""

from __future__ import annotations

from tests.conftest import TENANT_A_KEY, auth, wait_for_job

KB = "kb_ctx"


def _long_document() -> str:
    sections = []
    for number in range(1, 41):
        topic = f"topik{number:02d}"
        body = " ".join(f"kalimat penjelas {topic} nomor {i} tentang prosedur kerja harian." for i in range(18))
        sections.append(f"## Bagian {number}\n{topic} {body}")
    return "# Pedoman Operasional\n\n" + "\n\n".join(sections)


def _index(client, document_id: str, text: str, key: str = TENANT_A_KEY, kb: str = KB) -> dict:
    response = client.post(
        "/api/v1/knowledge/index",
        json={"document_id": document_id, "knowledge_base_id": kb, "document_name": f"{document_id}.md", "text": text},
        headers=auth(key),
    )
    assert response.status_code == 202, response.text
    return wait_for_job(client, document_id, key=key)


def _lexical(settings) -> None:
    settings.reranker_provider = "lexical"
    settings.reranker_enabled = True


def test_konteks_memakai_tetangga_hasil_bukan_awal_dokumen(client, settings):
    _lexical(settings)
    record = _index(client, "doc_pedoman", _long_document())
    assert record["chunks"] > 8, "dokumen uji harus cukup besar untuk tidak disertakan utuh"
    response = client.post(
        "/api/v1/query",
        json={"query": "jelaskan topik30", "knowledge_base_id": KB, "options": {"top_k": 1}},
        headers=auth(TENANT_A_KEY),
    )
    data = response.json()["data"]
    chunk_ids = [source["chunk_id"] for source in data["sources"]]
    positions = sorted(int(chunk.rsplit("_", 1)[-1]) for chunk in chunk_ids)
    assert "chunk_0001" not in chunk_ids, "dokumen besar tidak boleh dilengkapi dari bagian pertama"
    assert len(positions) >= 2 and positions == list(range(positions[0], positions[-1] + 1)), (
        "hasil + tetangganya harus bersambung"
    )
    assert data["usage"]["best_score"] > 0
    assert data["usage"]["relevance_gate"] == "lexical"


def test_pertanyaan_di_luar_knowledge_ditolak_sebelum_llm(client, settings):
    _lexical(settings)
    _index(client, "doc_pedoman", _long_document())
    response = client.post(
        "/api/v1/query",
        json={"query": "Berapa harga saham perusahaan di bursa tahun 2099?", "knowledge_base_id": KB},
        headers=auth(TENANT_A_KEY),
    )
    data = response.json()["data"]
    assert data["grounded"] is False
    assert data["no_answer_reason"] in ("below_threshold", "no_candidates")


def test_sumber_menandai_yang_dikutip(client, settings):
    _lexical(settings)
    _index(client, "doc_cuti", "# SOP Cuti\nJatah cuti tahunan adalah 12 hari kerja per tahun.")
    data = client.post(
        "/api/v1/query",
        json={"query": "berapa jatah cuti tahunan", "knowledge_base_id": KB},
        headers=auth(TENANT_A_KEY),
    ).json()["data"]
    assert data["sources"][0]["index"] == 1
    assert data["sources"][0]["cited"] is True, "mock LLM mengutip [1]"


def test_riwayat_percakapan_diterima_dan_pertanyaan_lanjutan_dicari_bersama(client, settings):
    _lexical(settings)
    _index(client, "doc_cuti", "# SOP Cuti\nJatah cuti tahunan adalah 12 hari kerja per tahun.")
    data = client.post(
        "/api/v1/query",
        json={
            "query": "kalau yang tahunan?",
            "knowledge_base_id": KB,
            "history": [
                {"role": "user", "content": "Berapa jatah cuti?"},
                {"role": "assistant", "content": "Ada beberapa jenis cuti [1]."},
            ],
        },
        headers=auth(TENANT_A_KEY),
    ).json()["data"]
    assert data["usage"]["search_query"].startswith("Berapa jatah cuti?")
    assert data["sources"], "pertanyaan lanjutan harus menemukan dokumen lewat pertanyaan sebelumnya"


def test_kunci_proyek_tidak_bisa_menghapus_dokumen_kb_lain(client):
    _index(client, "doc_bersama", "# Dokumen A\nIsi proyek A tentang anggaran.", kb="kb_proyek_a")
    created = client.post(
        "/api/v1/settings/api-keys",
        json={"label": "Proyek B", "permissions": ["read", "write"], "knowledge_base_ids": ["kb_proyek_b"]},
        headers=auth(TENANT_A_KEY),
    )
    key = created.json()["data"]["key"]
    refused = client.delete("/api/v1/knowledge/doc_bersama", headers=auth(key))
    assert refused.status_code == 404
    listed = client.get("/api/v1/knowledge", headers=auth(key)).json()["data"]
    assert listed["count"] == 0, "kunci proyek B tidak boleh melihat dokumen proyek A"
    still = client.get("/api/v1/knowledge/doc_bersama", headers=auth(TENANT_A_KEY))
    assert still.status_code == 200 and still.json()["data"]["status"] == "completed"
