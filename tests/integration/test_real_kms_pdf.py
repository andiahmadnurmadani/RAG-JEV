"""Uji dengan berkas NYATA: PDF "Struktur Lengkap Database KMS Telin" (20 halaman, 50k karakter).

Keluhan aslinya berasal dari berkas ini: jawabannya bilang "chunk terpotong / info tidak
ditemukan di konteks". Uji ini mengulang jalur yang sama (unggah lalu tanya) dan memastikan
tidak ada baris yang hilang serta seluruh bagian dokumen sampai ke model. Berkasnya milik
mesin pengembang, jadi uji ini DILEWATI kalau berkasnya tidak ada (mis. di CI).
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from tests.conftest import TENANT_A_KEY, auth, wait_for_job

PDF = Path(r"C:\Users\Andi Ahmad Nurmadani\Downloads\Struktur Lengkap Database KMS Telin (2).pdf")
KB = "kb_real_pdf"

pytestmark = pytest.mark.skipif(not PDF.exists(), reason="berkas PDF kasus tidak ada di mesin ini")


def _index(client) -> dict:
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_kms_real",
            "document_name": PDF.name,
            "knowledge_base_id": KB,
            "content_base64": base64.b64encode(PDF.read_bytes()).decode("ascii"),
            "replace": True,
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 202, response.text
    record = wait_for_job(client, "doc_kms_real")
    assert record["status"] == "completed", record
    return record


def test_every_page_and_every_table_row_survives_ingestion(client):
    from app.parsing.parser import parse_document
    from app.rag.chunker import chunk_document

    parsed = parse_document(PDF.read_bytes(), PDF.name)
    assert parsed.parser == "pymupdf"
    assert len(parsed.pages) >= 16
    assert not [page.page for page in parsed.pages if len(page.text.strip()) < 20], "ada halaman kosong"

    chunks = chunk_document(parsed, document_id="d", chunk_size=700, chunk_overlap=100)
    assert len(chunks) > 1
    joined = "\n".join(chunk.content for chunk in chunks)
    source_lines = [line.strip() for line in parsed.text.splitlines() if len(line.strip()) > 25]
    missing = [line for line in source_lines if line not in joined]
    assert not missing, f"{len(missing)} baris hilang, contoh: {missing[:2]}"


def test_the_whole_document_reaches_the_model(client, settings):
    record = _index(client)
    chunks = int(record["chunks"])

    response = client.post(
        "/api/v1/query",
        json={"query": "Tolong jelaskan struktur lengkap database ini, tabel apa saja yang ada.", "knowledge_base_id": KB},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    usage = data["usage"]

    print("\n  potongan dokumen :", chunks)
    print("  potongan di konteks :", usage["context_chunks"], f"(+{usage['context_expanded_chunks']} pelengkap)")
    print("  context_tokens   :", usage["context_tokens"])
    print("  kelengkapan      :", usage["document_coverage"][0])

    assert usage["context_chunks"] == chunks, "tidak semua bagian dokumen sampai ke model"
    coverage = usage["document_coverage"][0]
    assert coverage["total"] == chunks
    assert coverage["complete"] is True, coverage
    assert usage["context_tokens"] <= settings.context_token_budget
