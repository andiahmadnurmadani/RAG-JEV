"""Dokumen besar: seluruh isinya harus sampai ke konteks, bukan hanya potongan teratas.

Keluhan yang melahirkan berkas ini: PDF struktur database 20 halaman diunggah, ditanya, dan
jawabannya bilang "chunk terpotong / info tidak ditemukan di konteks" - padahal datanya ada di
indeks. Sebabnya dua angka: ``final_top_k=5`` dan ``max_chunks_per_document=3``, jadi model
hanya melihat 3 dari 20 bagian. Test di sini mengunci perilaku barunya.
"""

from __future__ import annotations

import base64

from tests.conftest import TENANT_A_KEY, auth, wait_for_job

KB = "kb_large"


def _build_document(sections: int = 40, rows: int = 10) -> str:
    """Dokumen panjang menyerupai dump struktur database: judul, prosa, tabel per bagian."""

    parts = ["# Struktur Lengkap Database KMS", "", "Dokumen ini memuat seluruh tabel inti.", ""]
    for index in range(1, sections + 1):
        parts.append(f"## Tabel_{index:02d}")
        parts.append(f"Bagian {index} menjelaskan tabel Tabel_{index:02d} beserta seluruh kolomnya.")
        parts.append("")
        parts.append("| kolom | tipe | null | keterangan |")
        for row in range(1, rows + 1):
            parts.append(
                f"| kolom_{index:02d}_{row:02d} | varchar({row * 10}) | YES | kolom ke-{row} pada Tabel_{index:02d} |"
            )
        parts.append("")
        parts.append(f"Catatan bagian {index}: kunci utama kolom_{index:02d}_01, indeks unik kolom_{index:02d}_02.")
        parts.append("")
    return "\n".join(parts)


def _index(client, text: str, *, name: str = "struktur-lengkap.md", document_id: str = "doc_large") -> dict:
    payload = {
        "document_id": document_id,
        "document_name": name,
        "knowledge_base_id": KB,
        "content_base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "replace": True,
    }
    response = client.post("/api/v1/knowledge/index", json=payload, headers=auth(TENANT_A_KEY))
    assert response.status_code == 202, response.text
    return wait_for_job(client, document_id)


def _answer(settings, *, query: str, top_k: int = 4):
    """Jalankan pipeline langsung supaya teks konteks yang dikirim ke model bisa diperiksa."""

    from app.api.deps import build_services
    from app.core.tenant import TrustedContext

    services = build_services(settings)
    context = TrustedContext(
        user_id="user_a", organization_id="org_a", application_id="app_a", permissions=["read", "write"]
    )
    return services.rag.answer(query=query, context=context, knowledge_base_id=KB, top_k=top_k)


def test_a_whole_document_reaches_the_model_not_just_the_top_chunks(settings, client):
    record = _index(client, _build_document())
    assert record["status"] == "completed", record
    chunks = int(record["chunks"])
    assert chunks >= 10, f"dokumen uji harus terpecah banyak, dapat {chunks}"

    # top_k kecil: pencarian hanya menemukan sebagian, jadi sisanya WAJIB datang dari pelengkap.
    result = _answer(settings, query="Sebutkan semua tabel yang ada di dokumen ini.", top_k=4)

    usage = result.usage
    assert usage["retrieved_chunks"] == 4, usage
    assert usage["context_chunks"] == chunks, usage
    assert usage["context_expanded_chunks"] == chunks - 4, usage
    assert result.document_coverage, usage
    assert result.document_coverage[0]["complete"] is True, result.document_coverage

    # Bukti isi: fakta yang hanya ada di bagian TERAKHIR ikut ke konteks yang dikirim ke model.
    assert result.context is not None
    assert "kolom_40_10" in result.context.text
    assert "Catatan bagian 40" in result.context.text
    assert "bagian 40" in result.context.text               # penanda urutan dokumen
    assert "lengkap (seluruh bagian, urut)" in result.context.text

    # Sumber yang bisa dikutip mencakup potongan terakhir juga.
    assert any(source["chunk_id"] == f"chunk_{chunks:04d}" for source in result.sources)


def test_the_http_response_reports_how_much_of_the_document_was_used(client):
    record = _index(client, _build_document(sections=30, rows=8), name="struktur-http.md", document_id="doc_http")
    chunks = int(record["chunks"])

    response = client.post(
        "/api/v1/query",
        json={"query": "Ringkas tabel yang ada.", "knowledge_base_id": KB, "options": {"top_k": 4}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    usage = response.json()["data"]["usage"]

    assert usage["context_chunks"] == chunks, usage
    assert usage["context_expanded_chunks"] >= 1, usage
    coverage = usage["document_coverage"]
    assert coverage and coverage[0]["total"] == chunks
    assert coverage[0]["complete"] is True
    assert coverage[0]["included"] == chunks


def test_a_document_larger_than_the_budget_is_reported_as_partial(settings, client):
    """Kalau dokumennya melebihi anggaran token, kelengkapannya dilaporkan apa adanya."""

    settings.context_max_tokens = 900          # anggaran kecil supaya pasti terpotong
    record = _index(client, _build_document(), document_id="doc_huge")
    chunks = int(record["chunks"])

    result = _answer(settings, query="Ringkas seluruh isi dokumen ini.", top_k=4)

    assert result.document_coverage, result.usage
    item = result.document_coverage[0]
    assert item["total"] == chunks
    assert item["included"] < item["total"], item
    assert item["complete"] is False, item

    # Model diberi tahu keadaannya, dan diberi tahu bahwa itu hanya soal konteks ini.
    assert result.context is not None
    assert "DOCUMENT_COVERAGE" in result.context.text
    assert f"sebagian ({item['included']} dari {item['total']} bagian)" in result.context.text
    assert "bukan daftar seluruh basis pengetahuan" in result.context.text

    # Potongan paling awal tetap di depan: pelengkap tidak membalik urutan.
    assert result.context.used
    assert result.context.used[0].chunk_id == "chunk_0001"


def test_expansion_can_be_switched_off(settings, client):
    """Pemasangan yang ingin hemat token bisa mematikan pelengkap dokumen."""

    settings.context_expand_documents = False
    record = _index(client, _build_document(sections=30, rows=8), document_id="doc_off")
    chunks = int(record["chunks"])

    result = _answer(settings, query="Sebutkan semua tabel.", top_k=4)

    assert result.usage["context_chunks"] == 4, result.usage
    assert result.usage["context_expanded_chunks"] == 0
    assert result.document_coverage[0]["complete"] is False
    assert result.document_coverage[0]["total"] == chunks


def test_expansion_stays_inside_the_tenant(settings, client):
    """Pelengkap dokumen tidak boleh menembus batas tenant, walau document_id-nya ditebak."""

    from app.qdrant import repository

    document = _build_document(sections=6, rows=5)
    _index(client, document, name="tenant-a.md", document_id="doc_tenant_a")
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_tenant_b",
            "document_name": "tenant-b.md",
            "knowledge_base_id": KB,
            "content_base64": base64.b64encode(document.encode("utf-8")).decode("ascii"),
        },
        headers=auth("test-key-org-b"),
    )
    assert response.status_code == 202, response.text
    wait_for_job(client, "doc_tenant_b", key="test-key-org-b")

    assert repository.list_document_chunks(
        settings, organization_id="org_b", document_id="doc_tenant_a", knowledge_base_id=KB
    ) == []
    assert repository.list_document_chunks(
        settings, organization_id="org_a", document_id="doc_tenant_a", knowledge_base_id=KB
    )

    result = _answer(settings, query="Tabel apa saja yang ada?", top_k=4)
    assert all(source["document_id"] == "doc_tenant_a" for source in result.sources)
    assert result.document_coverage and result.document_coverage[0]["document_id"] == "doc_tenant_a"
