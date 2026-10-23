"""Permintaan ringkasan umum ("buat ringkasan 5 poin utama") dijawab dari SEMUA dokumen.

Pertanyaan seperti itu tidak menyebut topik apa pun, jadi pencarian kemiripan biasa tidak
punya pegangan: skornya rendah dan dulu jawabannya "tidak ditemukan", atau hanya satu-dua
dokumen yang kebetulan mirip yang dibaca. Di sini dikunci bahwa:

* ringkasan umum memakai ringkasan (atau isi awal) setiap dokumen di knowledge base;
* pilihan dokumen dari pemakai tetap dihormati;
* ringkasan bertopik ("ringkas soal cuti") tetap lewat pencarian biasa.
"""

from __future__ import annotations

import base64

import pytest

from tests.conftest import TENANT_A_KEY, auth, wait_for_job
from tests.integration.test_document_summary import _services, _SummaryClient

KB = "kb_overview"


def _long(title: str, topic: str) -> str:
    # Cukup panjang supaya dokumennya tidak otomatis disertakan utuh ke konteks.
    parts = [f"# {title}"]
    for number in range(1, 61):
        parts.append(f"## Pasal {number}\n\nKetentuan {topic} nomor {number} berlaku bagi seluruh pegawai tetap "
                     f"dan pegawai kontrak di unit kerja masing-masing sesuai keputusan direksi tahun berjalan.")
    return "\n\n".join(parts)


DOCS = {
    "doc_cuti": ("cuti.md", _long("Kebijakan Cuti", "cuti tahunan")),
    "doc_lembur": ("lembur.md", _long("Aturan Lembur", "upah lembur")),
    "doc_absen": ("absensi.md", _long("Absensi Pegawai", "jam masuk")),
}


@pytest.fixture()
def settings(settings):
    # Seperti produksi: reranker leksikal dan gerbang relevansi aktif. Kalimat "buat ringkasan
    # 5 poin utama" tidak berbagi kata dengan isi dokumen, jadi gerbang ini yang dulu menolaknya.
    settings.reranker_enabled = True
    settings.reranker_provider = "lexical"
    settings.relevance_threshold = 0.35
    settings.rerank_threshold = 0.35
    settings.min_relevance = 0.28
    return settings




def _index_all(client):
    for document_id, (name, text) in DOCS.items():
        response = client.post(
            "/api/v1/knowledge/index",
            json={
                "document_id": document_id,
                "document_name": name,
                "knowledge_base_id": KB,
                "content_base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "replace": True,
            },
            headers=auth(TENANT_A_KEY),
        )
        assert response.status_code == 202, response.text
    for document_id in DOCS:
        record = wait_for_job(client, document_id, timeout=90.0)
        assert record["status"] == "completed", record


def _ask(client, query, **options):
    body = {"query": query, "knowledge_base_id": KB, "options": {"top_k": 4, **options}}
    response = client.post("/api/v1/query", json=body, headers=auth(TENANT_A_KEY))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_a_generic_summary_request_reads_every_document(client, settings):
    llm = _SummaryClient(summary="Ringkasan isi [1].")
    _services(settings, client, llm)
    _index_all(client)

    data = _ask(client, "buat ringkasan 5 poin utama")

    assert data["answer"] and "tidak ditemukan" not in data["answer"].lower(), data
    sent = llm.calls[-1]
    for name, _ in DOCS.values():
        assert name in sent, f"{name} harus ikut dibaca saat ringkasan umum"


def test_a_generic_summary_respects_the_picked_documents(client, settings):
    llm = _SummaryClient(summary="Ringkasan isi [1].")
    _services(settings, client, llm)
    _index_all(client)

    _ask(client, "rangkum isi dokumen ini", document_ids=["doc_cuti"])

    sent = llm.calls[-1]
    assert "cuti.md" in sent
    assert "lembur.md" not in sent and "absensi.md" not in sent


def test_a_generic_summary_is_reported_as_such(client, settings):
    _services(settings, client, _SummaryClient(summary="Ringkasan isi [1]."))
    _index_all(client)

    usage = _ask(client, "buat ringkasan 5 poin utama")["usage"]

    assert usage["relevance_gate"] == "summary_overview", usage
    assert usage["summary_overview"] == {"documents": 3, "available": 3}, usage
    assert {item["document_id"] for item in usage["document_coverage"]} == set(DOCS)


def test_a_summary_about_one_topic_still_searches(client, settings):
    llm = _SummaryClient(summary="Ringkasan isi [1].")
    _services(settings, client, llm)
    _index_all(client)

    usage = _ask(client, "ringkas ketentuan upah lembur")["usage"]

    assert usage["relevance_gate"] != "summary_overview", usage
    assert usage.get("summary_overview") is None, usage
