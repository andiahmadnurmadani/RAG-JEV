"""Ringkasan knowledge turunan: dibuat saat upload, dipakai saat user minta ringkasan.

Yang dikunci di sini:

* saat dokumen diindeks, ringkasannya dibuat dan diindeks sebagai potongan tersendiri;
* pertanyaan "ringkas dokumen ini" menemukan potongan ringkasan itu dan menjawab dari sana;
* ringkasan **tidak** ikut bersaing pada pencarian biasa - kalau ikut, ia bisa mendesak
  potongan isi keluar dari ``top_k`` dan jawaban faktual kehilangan detail tanpa jejak;
* isi dokumen TIDAK pernah dikorbankan demi ringkasan (jaminan "tidak ada data hilang");
* dokumen besar diringkas bertahap (map-reduce), bukan dipotong di jendela pertama;
* ringkasan gagal tidak menggagalkan pengindeksan, dan alasannya dilaporkan.
"""

from __future__ import annotations

import base64

from tests.conftest import TENANT_A_KEY, auth, wait_for_job

KB = "kb_summary"


class _SummaryClient:
    """Klien LLM uji yang meringkas dengan menandai bagian-bagian yang dilihatnya."""

    def __init__(self, *, summary: str = "", truncate: bool = False) -> None:
        self.model = "uji-ringkas"
        self.calls: list = []
        self._summary = summary
        self._truncate = truncate

    def chat(self, messages, **kwargs):
        from app.rag.generator import LLMUsage

        user = messages[-1]["content"]
        self.calls.append(user)
        if self._summary:
            text = self._summary
        else:
            # Ringkasan default: sebutkan berapa blok yang terlihat, supaya uji bisa memeriksa
            # bahwa isi dokumen benar-benar dikirim ke model saat meringkas.
            text = "Ringkasan uji: " + str(user.count("<retrieved_document>")) + " bagian dibaca."
        limit = kwargs.get("max_tokens") or 2048
        return text, LLMUsage(
            input_tokens=200,
            output_tokens=limit if self._truncate else 40,
            latency_ms=1.0,
            model=self.model,
            finish_reason="length" if self._truncate else "stop",
        )

    def health(self) -> str:
        return "ok"


def _index(client, text: str, *, document_id: str = "doc_ringkas", name: str = "panduan.md") -> dict:
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
    return wait_for_job(client, document_id, timeout=90.0)


def _services(settings, client, llm=None):
    from app.api.deps import get_services

    services = get_services()
    if llm is not None:
        services.generator.rebind_llm_client(llm)
        services.indexing._generator = services.generator
    return services


def _document(sections: int = 5, repeat: int = 1) -> str:
    parts = ["# Panduan Layanan", "", "Dokumen ini menjelaskan prosedur layanan."]
    for index in range(1, sections + 1):
        parts.append(f"## Bagian {index}")
        parts.append(
            f"Bagian {index} mengatur ketentuan khusus bernama ketentuan_{index:02d}. "
            + ("Penjelasan rinci beserta contoh penerapannya. " * repeat)
        )
        parts.append("")
    return "\n".join(parts)


def _content_chunks(settings, text: str, document_id: str = "doc_hitung") -> int:
    """Berapa potongan ISI yang seharusnya terbentuk dari teks ini."""
    from app.parsing.parser import ParsedDocument, ParsedPage
    from app.rag.chunker import chunk_document

    parsed = ParsedDocument(document_name="hitung.md", pages=[ParsedPage(page=1, text=text)])
    return len(
        chunk_document(
            parsed,
            document_id=document_id,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            min_chunk_tokens=settings.min_chunk_tokens,
        )
    )


def test_a_summary_is_created_and_indexed_with_the_document(client, settings):
    services = _services(settings, client)
    llm = _SummaryClient(summary="Ringkasan resmi: dokumen ini mengatur prosedur layanan.")
    services.generator.rebind_llm_client(llm)
    services.indexing._generator = services.generator

    text = _document()
    expected_chunks = _content_chunks(settings, text)
    record = _index(client, text, document_id="doc_ringkas_1")
    assert record["status"] == "completed", record
    assert record["summary"], "ringkasan harus dibuat saat pengindeksan"
    assert "prosedur layanan" in record["summary"]
    assert record["summary_tokens"] > 0

    # Isi dokumen TIDAK berkurang karena ringkasan: jumlah potongan isi tetap seperti semula,
    # dan potongan ringkasan tidak ikut dihitung sebagai isi.
    assert record["chunks"] == expected_chunks, record

    # Ringkasan bisa diambil sebagai potongan tersendiri dari indeks.
    from app.qdrant import repository

    payload = repository.get_summary_chunk(
        settings, organization_id="org_a", document_id="doc_ringkas_1", knowledge_base_id=KB
    )
    assert payload, "potongan ringkasan harus ada di indeks"
    assert payload["is_summary"] is True
    assert "prosedur layanan" in payload["content"]

    # Jumlah "isi" yang dilaporkan penyimpanan juga tidak ikut menghitung ringkasan.
    counted = repository.count_document(
        settings, organization_id="org_a", document_id="doc_ringkas_1", include_summary=False
    )
    assert counted == expected_chunks, counted
    with_summary = repository.count_document(
        settings, organization_id="org_a", document_id="doc_ringkas_1", include_summary=True
    )
    assert with_summary == expected_chunks + 1, with_summary


def test_asking_for_a_summary_uses_the_stored_summary(client, settings):
    services = _services(settings, client)
    services.generator.rebind_llm_client(_SummaryClient(summary="Ringkasan resmi: tiga ketentuan utama."))
    services.indexing._generator = services.generator
    _index(client, _document(), document_id="doc_ringkas_2")

    response = client.post(
        "/api/v1/query",
        json={"query": "Tolong ringkas dokumen ini.", "knowledge_base_id": KB, "options": {"top_k": 4}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    usage = data["usage"]
    assert usage["context_summary_chunks"] >= 1, usage
    # Isi dokumen tetap ikut: ringkasan melengkapi, bukan menggantikan.
    assert usage["context_chunks"] >= 1, usage
    assert any(source["chunk_id"] == "chunk_summary" for source in data["sources"]), data["sources"]


def test_the_summary_does_not_compete_in_normal_search(client, settings):
    """Pertanyaan faktual harus dijawab dari isi, bukan dari ringkasan yang sudah dipadatkan."""
    services = _services(settings, client)
    services.generator.rebind_llm_client(_SummaryClient(summary="Ringkasan: hanya menyebut garis besar."))
    services.indexing._generator = services.generator
    _index(client, _document(), document_id="doc_ringkas_3")

    found = client.post(
        "/api/v1/search",
        json={"query": "ketentuan_03", "knowledge_base_id": KB, "top_k": 8},
        headers=auth(TENANT_A_KEY),
    )
    assert found.status_code == 200, found.text
    results = found.json()["data"]["results"]
    assert results, "isi dokumen harus tetap bisa dicari"
    assert all(item["chunk_id"] != "chunk_summary" for item in results), (
        "ringkasan tidak boleh ikut bersaing di pencarian biasa"
    )


def test_the_content_is_never_dropped_in_favour_of_the_summary(client, settings):
    """Anggaran konteks kecil: isi tetap prioritas, ringkasan tidak boleh menggeser isi."""
    settings.context_max_tokens = 700
    services = _services(settings, client)
    services.generator.rebind_llm_client(_SummaryClient(summary="R" * 6000))  # ringkasan raksasa
    services.indexing._generator = services.generator
    _index(client, _document(sections=12), document_id="doc_ringkas_4")

    response = client.post(
        "/api/v1/query",
        json={"query": "Sebutkan ketentuan yang ada.", "knowledge_base_id": KB, "options": {"top_k": 4}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    usage = response.json()["data"]["usage"]
    assert usage["context_chunks"] >= 1, f"isi dokumen harus tetap masuk konteks: {usage}"


def test_a_large_document_is_summarized_in_stages(client, settings):
    """Dokumen besar: ringkasan dibuat bertahap, bukan berhenti di jendela pertama."""
    settings.summary_window_tokens = 300          # jendela kecil supaya pasti beberapa tahap
    settings.summary_max_tokens = 256
    services = _services(settings, client)
    llm = _SummaryClient()
    services.generator.rebind_llm_client(llm)
    services.indexing._generator = services.generator

    # Dokumen yang cukup panjang untuk terpecah menjadi beberapa potongan.
    text = _document(sections=40, repeat=30)
    assert _content_chunks(settings, text, "doc_hitung_besar") >= 3, "dokumen uji harus panjang"

    record = _index(client, text, document_id="doc_ringkas_besar")
    assert record["status"] == "completed", record
    assert record["summary"], record.get("summary_error")
    # Tahap map: satu panggilan per kelompok, lalu satu tahap reduce.
    assert len(llm.calls) >= 2, f"dokumen besar harus diringkas bertahap, panggilan={len(llm.calls)}"


def test_a_failed_summary_does_not_break_indexing(client, settings):
    """Ringkasan opsional: isi dokumen tetap terindeks, dan alasannya dilaporkan apa adanya."""
    services = _services(settings, client)

    class _Broken:
        model = "rusak"

        def chat(self, messages, **kwargs):
            raise RuntimeError("LLM sedang mati")

        def health(self) -> str:
            return "error"

    services.generator.rebind_llm_client(_Broken())
    services.indexing._generator = services.generator

    record = _index(client, _document(), document_id="doc_ringkas_gagal")
    assert record["status"] == "completed", record
    assert record["chunks"] >= 1, "isi dokumen harus tetap terindeks"
    assert not record["summary"]
    assert record["summary_error"], "kegagalan ringkasan harus dilaporkan, bukan disembunyikan"

    # Isi dokumennya tetap bisa dicari.
    found = client.post(
        "/api/v1/search",
        json={"query": "ketentuan_02", "knowledge_base_id": KB, "top_k": 5},
        headers=auth(TENANT_A_KEY),
    )
    assert found.json()["data"]["results"]


def test_summarizing_can_be_switched_off(client, settings):
    settings.document_summary_enabled = False
    services = _services(settings, client)
    services.generator.rebind_llm_client(_SummaryClient(summary="tidak dipakai"))
    services.indexing._generator = services.generator

    record = _index(client, _document(), document_id="doc_ringkas_mati")
    assert record["status"] == "completed"
    assert not record["summary"]
    assert "dimatikan" in (record["summary_error"] or "")
    settings.document_summary_enabled = True


def test_the_summary_respects_the_tenant(client, settings):
    """Ringkasan tidak boleh terbaca lintas tenant walau document_id-nya ditebak."""
    from app.qdrant import repository

    services = _services(settings, client)
    services.generator.rebind_llm_client(_SummaryClient(summary="Ringkasan tenant A rahasia."))
    services.indexing._generator = services.generator
    _index(client, _document(), document_id="doc_ringkas_tenant")

    stolen = repository.get_summary_chunk(
        settings, organization_id="org_b", document_id="doc_ringkas_tenant", knowledge_base_id=KB
    )
    assert stolen is None, "ringkasan tenant lain tidak boleh terbaca"
