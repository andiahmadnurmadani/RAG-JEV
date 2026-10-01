"""PDF & tabel besar: isinya harus utuh dan potongannya tidak boleh terbelah di tengah baris.

Dua cara kehilangan data yang diuji di sini:

* PDF dibaca tanpa pengenalan tata letak, sehingga kolom tertukar dan tabel berspasi tidak
  bisa dipotong per baris (satu tabel jadi potongan raksasa, atau barisnya terbelah dua);
* potongan tabel kehilangan baris kepala, sehingga kolom ``Viewed_Date`` di potongan ke-12
  tidak lagi bisa dipasangkan dengan nilainya.
"""

from __future__ import annotations

import builtins

import pytest

from app.parsing.parser import parse_document
from app.rag.chunker import chunk_document, chunk_text, estimate_tokens

HEADERS = ["kolom", "tipe", "null", "keterangan"]


def _pdf_bytes(rows: int = 40) -> bytes:
    """PDF sederhana: judul, prosa, lalu tabel berkolom sejajar (tanpa garis)."""

    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((60, 60), "Struktur Lengkap Database KMS", fontsize=12)
    page.insert_text((60, 80), "Dokumen ini memuat seluruh tabel inti.", fontsize=9)
    y = 110.0
    x_positions = [60.0, 220.0, 300.0, 350.0]
    for column, label in enumerate(HEADERS):
        page.insert_text((x_positions[column], y), label, fontsize=8)
    y += 14
    for index in range(1, rows + 1):
        cells = [f"kolom_{index:02d}", "varchar(255)", "YES", f"catatan-{index:02d}"]
        for column, value in enumerate(cells):
            page.insert_text((x_positions[column], y), value, fontsize=8)
        y += 12
    payload = document.tobytes()
    document.close()
    return payload


def test_pdf_is_parsed_with_layout_and_no_cell_is_lost():
    parsed = parse_document(_pdf_bytes(), "struktur.pdf")
    assert parsed.parser == "pymupdf"
    text = parsed.text
    assert "Struktur Lengkap Database KMS" in text
    for index in (1, 20, 40):
        assert f"kolom_{index:02d}" in text, "nilai sel hilang saat ekstraksi"
        assert f"catatan-{index:02d}" in text


def test_pdf_tables_become_row_text_when_tables_are_detected():
    """Kalau tabel terdeteksi, barisnya dirender ``| a | b |`` supaya bisa dipotong per baris."""

    parsed = parse_document(_pdf_bytes(), "struktur.pdf")
    if "|" not in parsed.text:
        # Deteksi tabel bergantung geometri berkasnya; tanpa deteksi pun isinya tidak hilang
        # (diuji di atas) - dan pemotongan per baris tetap berlaku lewat deteksi sesudah ini.
        assert "kolom_01" in parsed.text
        return
    rows = [line for line in parsed.text.splitlines() if line.strip().startswith("|")]
    assert len(rows) >= 30
    assert all(row.count("|") >= 3 for row in rows)


def test_pdf_falls_back_to_pypdf_when_pymupdf_is_not_installed(monkeypatch):
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name in {"pymupdf", "fitz"}:
            raise ImportError("diblokir untuk uji")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    parsed = parse_document(_pdf_bytes(rows=10), "struktur.pdf")
    assert parsed.parser == "pypdf"
    assert "kolom_05" in parsed.text


def test_space_separated_table_rows_are_never_split_across_chunks():
    """Tabel tanpa pemisah ``|`` (hasil pembaca PDF biasa) dipotong di batas baris saja."""

    lines = [f"kolom_{index:03d}  varchar(255)  YES  catatan baris {index}" for index in range(1, 121)]
    text = "Struktur tabel\n" + "\n".join(lines)
    chunks = chunk_text(text, document_id="doc_rows", chunk_size=120, chunk_overlap=0)
    assert len(chunks) > 1, "blok sepanjang ini harus terpecah"

    joined = "\n".join(chunk.content for chunk in chunks)
    for line in lines:
        assert line in joined, f"baris terbelah / hilang: {line}"
    kept = [line for chunk in chunks for line in chunk.content.splitlines() if line.startswith("kolom_")]
    assert len(kept) == len(lines)


def test_large_markdown_table_keeps_its_header_row_in_every_chunk():
    header = "| kolom | tipe | null |"
    rows = [f"| kolom_{index:03d} | varchar({index}) | YES |" for index in range(1, 201)]
    text = "## Tabel knowledge\n\n" + "\n".join([header] + rows)
    chunks = chunk_text(text, document_id="doc_table", chunk_size=150, chunk_overlap=0)
    table_chunks = [chunk for chunk in chunks if chunk.is_table]
    assert len(table_chunks) >= 3, "tabel besar harus terpecah beberapa potongan"
    for chunk in table_chunks:
        # Baris kepala harus jadi baris isi pertama (potongan pertama masih membawa judul bagian).
        body_lines = [line for line in chunk.content.splitlines() if line.strip() and not line.startswith("#")]
        assert body_lines and body_lines[0] == header, "baris kepala hilang dari potongan"

    seen_rows = [
        line
        for chunk in table_chunks
        for line in chunk.content.splitlines()
        if line.startswith("| kolom_")
    ]
    for row in rows:
        assert row in seen_rows, f"baris tabel hilang: {row}"


def test_a_giant_line_inside_a_row_block_is_cut_by_the_character_limit():
    """Baris raksasa di dalam blok tersusun-baris dipotong keras sebagai pilihan terakhir."""

    lines = [f"kolom_{index:03d}  varchar  YES  catatan" for index in range(1, 20)]
    lines.append("x" * 4000)
    chunks = chunk_text("\n".join(lines), document_id="doc_giant", chunk_size=100, chunk_overlap=0)
    assert len(chunks) >= 2
    # Tidak ada potongan yang memuat deretan 'x' lebih panjang dari limit karakter (100*4).
    assert max(chunk.content.count("x") for chunk in chunks) <= 400
    kept = "".join(chunk.content.replace("\n", "") for chunk in chunks)
    assert kept.count("x") == 4000, "baris raksasa tidak boleh ada yang hilang"


def test_tokens_are_not_underestimated_for_text_without_spaces():
    """Blob tanpa spasi tetap dihitung mahal, supaya anggaran konteks tidak ditembus."""

    from app.parsing.parser import ParsedDocument, ParsedPage
    from app.rag.chunker import chunk_document

    blob = "A" * 6000                       # tanpa spasi sama sekali
    assert estimate_tokens(blob) >= 900, estimate_tokens(blob)
    chunks = chunk_document(
        ParsedDocument(document_name="blob.md", pages=[ParsedPage(page=1, text=blob)]),
        document_id="doc_blob",
        chunk_size=200,
        chunk_overlap=0,
    )
    assert len(chunks) >= 3, "blob besar harus terpecah beberapa potongan"
    assert sum(chunk.token_count for chunk in chunks) >= 900


def test_chunk_accounting_matches_the_source_for_a_dense_table():
    from app.parsing.parser import ParsedDocument, ParsedPage

    rows = [f"| k{i} | {i} |" for i in range(1, 400)]
    text = "| nama | nilai |\n" + "\n".join(rows)
    chunks = chunk_document(
        ParsedDocument(document_name="tabel.md", pages=[ParsedPage(page=1, text=text)]),
        document_id="doc_dense",
        chunk_size=200,
        chunk_overlap=0,
    )
    tokens = sum(item.token_count for item in chunks)
    assert tokens >= estimate_tokens(text)
