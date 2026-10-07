"""Aksara asing & teks sampah: dibuang bila selipan model, dipertahankan bila fakta dokumen.

Latar nyata dari data produksi (2.840 potongan dipindai):
- 37 potongan berisi byte biner PDF yang gagal di-parse (``%PDF-1.4``, ``endstream``);
- 29 potongan ringkasan memuat aksara Han yang diselipkan model ("dokumen finals完整的");
- 15 potongan memang memuat aksara Han SAH (label skema: ``地`` = shield).

Dua yang pertama adalah cacat; yang ketiga adalah isi dokumen. Uji ini memastikan perbaikannya
membedakan keduanya - filter yang membuang semua aksara asing akan merusak data yang sah.
"""

from __future__ import annotations

from app.parsing.sanitize import (
    clean_text,
    foreign_tokens,
    looks_like_binary_garbage,
    strip_foreign_tokens,
)

# Dump PDF nyata: header + kata kunci format + byte tak ter-decode.
PDF_DUMP = "%PDF-1.4\n5 0 obj <</Length 6 0 R/Filter /FlateDecode>> stream\n\xed]y\x07\xbb\x9c\x00\xff" * 4
TEKS_NORMAL = (
    "Laporan Tahunan bank bjb 2025 memuat total aset dan kewajiban. "
    "Rapat agenda membahas remunerasi direksi serta rencana kerja."
)


# --------------------------------------------------------------- sampah biner

def test_a_binary_dump_is_recognised_as_garbage():
    garbage, reason = looks_like_binary_garbage(PDF_DUMP)
    assert garbage is True
    assert reason


def test_normal_prose_is_not_garbage():
    assert looks_like_binary_garbage(TEKS_NORMAL)[0] is False
    # Satu-dua karakter aneh masih wajar (berkas rusak sebagian) - jangan dibuang.
    assert looks_like_binary_garbage("Nilai aset 100 triliun \ufffd kecil.")[0] is False


def test_a_document_that_discusses_pdf_is_not_mistaken_for_a_binary_dump():
    """Dokumen teknis yang membahas format PDF menyebut %PDF/endobj secara sah."""
    tentang_pdf = (
        "Format %PDF-1.4 dipakai luas. Penanda endobj menutup objek, dan /FlateDecode "
        "menandai aliran terkompresi. Spesifikasi PDF menjelaskan urutannya."
    )
    assert looks_like_binary_garbage(tentang_pdf)[0] is False


def test_clean_text_removes_control_characters_but_keeps_content():
    assert clean_text("Halo\x00 dunia\x07.") == "Halo dunia."
    assert clean_text("Nilai    aset\n\n\n\nbesar") == "Nilai aset\n\nbesar"


def test_clean_text_keeps_markdown_tables_intact():
    tabel = "| Pin | Ke |\n|---|---|\n| VCC | 3V3 |"
    assert clean_text(tabel) == tabel


# --------------------------------------------------------------- aksara asing

def test_a_model_slip_is_detected_and_removed():
    konteks = "Dokumen finals perakitan, wiring, dan firmware."
    jawaban = "Dokumen finals完整的 perakitan, wiring, dan firmware selesai."
    tokens = foreign_tokens(jawaban, allowed=[konteks])
    assert tokens, "aksara Han yang diselipkan model harus terdeteksi"
    bersih = strip_foreign_tokens(jawaban, tokens)
    assert "完" not in bersih and "整" not in bersih
    # Kata Latin yang MENEMPEL pada aksara asing tidak boleh ikut hilang. Ini bug nyata yang
    # ditemukan saat menguji: rentang yang diperlebar ke huruf Latin ikut menghapus "perakitan".
    assert "perakitan" in bersih, "kata Latin yang menempel tidak boleh ikut terhapus"
    assert "firmware" in bersih


def test_a_latin_word_glued_to_foreign_characters_survives():
    """``perakitan完整的`` -> ``perakitan`` harus utuh, aksara asingnya saja yang hilang."""
    konteks = "Dokumen final perakitan memuat wiring."
    jawaban = "Dokumen final perakitan完整的 memuat wiring [1]."
    bersih = strip_foreign_tokens(jawaban, foreign_tokens(jawaban, allowed=[konteks]))
    assert "perakitan" in bersih
    assert "完" not in bersih and "整" not in bersih
    assert "[1]" in bersih, "sitasi tidak boleh hilang"


def test_foreign_text_that_is_really_in_the_document_is_kept():
    """Label skema beraksara Han adalah FAKTA dokumen, bukan cacat."""
    konteks = "| 地 | Shield kabel, atau kosong |"
    jawaban = "| 地 | Shield kabel, atau kosong |"
    tokens = foreign_tokens(jawaban, allowed=[konteks])
    assert tokens == [], "aksara yang ada di dokumen tidak boleh dianggap selipan"
    assert strip_foreign_tokens(jawaban, tokens) == jawaban


def test_cleanup_does_not_break_a_markdown_table():
    jawaban = "## Pin\n\n| Pin | Ke |\n|---|---|\n| VCC | 3V3 |\n| 地 | GND |"
    konteks = "| Pin | Ke |\n|---|---|\n| VCC | 3V3 |"
    bersih = strip_foreign_tokens(jawaban, foreign_tokens(jawaban, allowed=[konteks]))
    assert "|---|---|" in bersih, "garis pemisah tabel Markdown harus utuh"
    assert "| VCC | 3V3 |" in bersih


# --------------------------------------------------------------- jalur generator

def test_the_generator_strips_a_model_slip_but_not_document_facts(settings):
    from app.rag.generator import Generator

    generator = Generator(settings)
    konteks = "Dokumen finals perakitan dan wiring."
    teks, dibuang = generator._strip_foreign_script("Dokumen finals完整的 perakitan.", konteks)
    assert "完" not in teks and dibuang

    konteks_skema = "| 地 | Shield |"
    teks2, dibuang2 = generator._strip_foreign_script("| 地 | Shield |", konteks_skema)
    assert teks2 == "| 地 | Shield |"
    assert dibuang2 == []


def test_the_summary_prompt_forbids_foreign_scripts():
    from app.rag.generator import SUMMARY_PROMPT_EN, SUMMARY_PROMPT_ID, SYSTEM_PROMPT

    for prompt in (SYSTEM_PROMPT, SUMMARY_PROMPT_ID, SUMMARY_PROMPT_EN):
        lowered = prompt.lower()
        assert "latin" in lowered, "prompt harus menyebut aksara Latin"
        assert "chinese" in lowered or "china" in lowered, "prompt harus menyebut aksara China"


# --------------------------------------------------------------- jalur parsing

def test_a_pdf_served_as_text_is_not_stored_as_knowledge():
    """PDF yang dijawab server sebagai text/plain tidak boleh jadi potongan sampah."""
    from app.parsing.web import FetchedPage, WebFetchError, page_to_text

    page = FetchedPage(
        url="https://contoh.invalid/artikel.pdf",
        final_url="https://contoh.invalid/artikel.pdf",
        status=200,
        content_type="text/plain",  # header salah - isinya PDF
        content=b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF",
    )
    assert page.is_text is False, "isi PDF harus menang atas header text/plain"
    try:
        page_to_text(page)
    except Exception as exc:  # noqa: BLE001
        # Boleh gagal sebagai PDF tak terbaca, tetapi TIDAK boleh mengembalikan teks sampah.
        assert not isinstance(exc, AssertionError)
    else:
        raise AssertionError("dump PDF tidak boleh menghasilkan teks biasa")


# ------------------------------------------------- data lama yang sudah tersimpan

def test_garbage_chunks_already_in_the_index_are_dropped_at_read_time(settings):
    """Potongan sampah dari data LAMA dibuang di jalur baca, tanpa menyentuh produksi."""
    from app.rag.pipeline import RagPipeline
    from app.rag.retriever import Candidate

    bersih = Candidate(chunk_id="c1", document_id="d1", content="Laporan tahunan memuat total aset.")
    sampah = Candidate(chunk_id="c2", document_id="d1", content=PDF_DUMP)
    hasil = RagPipeline._drop_garbage_candidates([bersih, sampah])
    assert [c.chunk_id for c in hasil] == ["c1"], "hanya potongan sampah yang dibuang"


def test_a_stored_summary_with_a_model_slip_is_cleaned_when_read(settings):
    """Ringkasan lama yang memuat aksara selipan dibersihkan saat dibaca (data lama tidak diubah)."""
    from app.rag.pipeline import RagPipeline

    class FakeRetriever:
        pass

    pipeline = RagPipeline.__new__(RagPipeline)
    pipeline._settings = settings  # type: ignore[attr-defined]

    isi_dokumen = "Dokumen final perakitan memuat wiring dan firmware."
    ringkasan_lama = "Dokumen final perakitan完整的 memuat wiring dan firmware."
    bersih = pipeline._clean_stored_summary(ringkasan_lama, isi_dokumen, "d1")
    assert "完" not in bersih and "整" not in bersih
    assert "perakitan" in bersih, "kata Latin tidak boleh ikut hilang"

    # Ringkasan yang memang memuat aksara dari dokumen tidak diubah.
    ringkasan_sah = "| 地 | Shield kabel |"
    assert pipeline._clean_stored_summary(ringkasan_sah, "| 地 | Shield |", "d1") == ringkasan_sah
