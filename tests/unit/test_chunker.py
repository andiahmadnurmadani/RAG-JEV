"""Chunking: structure preservation, tables, overlap, page attribution (PRD 11)."""

from __future__ import annotations

from app.parsing.parser import ParsedDocument, ParsedPage
from app.rag.chunker import chunk_document, chunk_text, estimate_tokens


def _document(pages):
    return ParsedDocument(
        document_name="SOP Cuti.pdf",
        pages=[ParsedPage(page=index, text=text) for index, text in enumerate(pages, start=1)],
    )


def test_headings_are_tracked_in_the_section_path():
    parsed = _document(
        [
            "# SOP Cuti\n\n## Pengajuan\n\nKaryawan mengajukan cuti melalui sistem HR minimal 7 hari sebelumnya.\n",
            "## Persetujuan\n\nAtasan langsung menyetujui dalam 2 hari kerja.\n",
        ]
    )
    chunks = chunk_document(parsed, document_id="doc_1", chunk_size=120, chunk_overlap=0)
    sections = {chunk.section for chunk in chunks}
    assert any("SOP Cuti" in section and "Pengajuan" in section for section in sections)
    assert any("Persetujuan" in section for section in sections)
    assert all(chunk.document_id == "doc_1" for chunk in chunks)


def test_chunks_carry_the_page_they_came_from():
    page_one = " ".join(
        f"Halaman satu kalimat {index} membahas kebijakan kehadiran dan disiplin kerja karyawan tetap" for index in range(30)
    )
    page_two = " ".join(
        f"Halaman dua kalimat {index} membahas prosedur cuti tahunan dan formulir yang wajib dilampirkan" for index in range(30)
    )
    parsed = _document([page_one, page_two])
    chunks = chunk_document(parsed, document_id="doc_2", chunk_size=60, chunk_overlap=0)
    pages = {chunk.page for chunk in chunks}
    assert pages == {1, 2}


def test_a_table_is_not_split_when_it_fits_the_budget():
    table = "| Bulan | Penjualan |\n| Januari | 100000000 |\n| Februari | 120000000 |\n| Maret | 90000000 |"
    parsed = _document([f"# Laporan\n\n{table}\n"])
    chunks = chunk_document(parsed, document_id="doc_3", chunk_size=200, chunk_overlap=0)
    table_chunks = [chunk for chunk in chunks if chunk.is_table]
    assert table_chunks, "table blocks must be tagged"
    assert "| Maret | 90000000 |" in "".join(chunk.content for chunk in table_chunks)


def test_overlap_is_applied_between_neighbouring_chunks():
    text = " ".join(f"kalimat nomor {index} berisi informasi penting" for index in range(120))
    with_overlap = chunk_text(text, document_id="doc_4", chunk_size=80, chunk_overlap=40)
    without_overlap = chunk_text(text, document_id="doc_5", chunk_size=80, chunk_overlap=0)
    assert len(with_overlap) >= 1
    if len(with_overlap) > 1:
        assert with_overlap[1].content.split()[0] in without_overlap[0].content.split()


def test_offsets_are_within_the_source_page():
    page_text = "# Bagian\n\nParagraf pertama tentang cuti.\n\nParagraf kedua tentang lembur.\n"
    parsed = _document([page_text])
    chunks = chunk_document(parsed, document_id="doc_6", chunk_size=100, chunk_overlap=0)
    for chunk in chunks:
        assert 0 <= chunk.char_start < chunk.char_end <= len(page_text) + 1


def test_token_estimate_is_deterministic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("satu dua tiga") == estimate_tokens("satu dua tiga") > 0


def test_noise_tail_is_merged_not_emitted():
    parsed = _document(["# Judul\n\nIsi panjang tentang prosedur cuti tahunan di perusahaan ini.\n\nok.\n"])
    chunks = chunk_document(parsed, document_id="doc_7", chunk_size=200, chunk_overlap=0, min_chunk_tokens=40)
    assert all(chunk.token_count >= 1 for chunk in chunks)
    assert len(chunks) <= 2
