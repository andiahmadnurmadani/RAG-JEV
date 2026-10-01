"""Format berkas lama (.doc/.xls/.ppt biner) dan pemilihan parser.

Bukti yang diuji di sini:

* kontainer OLE2 dibaca tanpa ``olefile`` (daftar stream + isinya);
* ``.xls`` BIFF8: nama lembar, nilai sel, dan **sel tanggal** (bukan serial angka);
* ``.doc`` Word 97: teks utuh, termasuk dokumen dengan *piece table* (potongan ANSI +
  Unicode dalam satu berkas);
* ``.ppt`` PowerPoint 97: seluruh slide dan seluruh atom teksnya;
* isi berkas lebih dipercaya daripada ekstensi (RTF/HTML yang dinamai ``.doc``, dan
  stream OLE yang tidak cocok dengan namanya).

Berkas uji di ``tests/fixtures/legacy`` dibuat oleh ``scripts/make_legacy_samples.py``
(``.xls`` oleh xlwt; ``.doc``/``.ppt`` memakai penulis CFB sendiri). Tes dilewati dengan
pesan jelas bila berkas uji belum dibuat, bukan gagal palsu.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.parsing import doc_binary, formats, ole, parser, ppt_binary, tables, xls_binary

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "legacy"
LEGACY_SAMPLES = Path(__file__).resolve().parents[2] / "data" / "tmp" / "legacy"


def fixture(name: str) -> bytes:
    for base in (FIXTURES, LEGACY_SAMPLES):
        candidate = base / name
        if candidate.exists():
            return candidate.read_bytes()
    pytest.skip(f"berkas uji {name} belum dibuat - jalankan scripts/make_legacy_samples.py")


# --------------------------------------------------------------------------- #
# Katalog format
# --------------------------------------------------------------------------- #


def test_format_lama_tersedia_tanpa_pustaka_tambahan():
    for key in ("doc", "xls", "ppt"):
        spec = formats.BY_KEY[key]
        assert spec.requires_module == "", f"{key} tidak boleh butuh pustaka pihak ketiga"
        assert spec.requires_binary == ""
        assert formats.availability(spec) is True


def test_ekstensi_lama_dan_email_masuk_default():
    default = formats.default_extensions()
    for extension in (".doc", ".ppt", ".xls", ".eml", ".docm", ".pptm"):
        assert extension in default, f"{extension} harus aktif tanpa setelan khusus"


# --------------------------------------------------------------------------- #
# Kontainer OLE
# --------------------------------------------------------------------------- #


def test_kontainer_ole_membaca_stream_word():
    content = fixture("sop_cuti_lama.doc")
    container = ole.OleContainer(content)
    assert container.has("WordDocument")
    assert len(container.read("WordDocument")) > 500
    assert "WordDocument" in container.names()


def test_kontainer_ole_menolak_berkas_bukan_cfb():
    with pytest.raises(Exception):
        ole.OleContainer(b"bukan berkas OLE sama sekali")


# --------------------------------------------------------------------------- #
# .xls
# --------------------------------------------------------------------------- #


def test_xls_membaca_lembar_nilai_dan_tanggal():
    sheets = xls_binary.read_workbook(fixture("laporan_penjualan_lama.xls"))
    names = [sheet.name for sheet in sheets]
    assert names == ["Penjualan", "Ringkasan"]

    rows = sheets[0].rows()
    assert rows[0] == ["Tanggal", "Kode", "Produk", "Jumlah", "Harga Satuan", "Total"]
    # Sel tanggal harus jadi tanggal ISO, bukan serial Excel (46075 dan sejenisnya).
    assert rows[1][0] == "2026-08-02"
    assert rows[1][3] == "30"
    assert "Rp1.500.000" in [cell for row in rows for cell in row]   # teks mata uang utuh


def test_xls_lembar_kedua_tidak_kehilangan_kolom():
    result = tables.extract_tables(fixture("laporan_penjualan_lama.xls"), "laporan_penjualan_lama.xls")
    ringkasan = [table for table in result if table.sheet == "Ringkasan"][0]
    assert ringkasan.headers == ["Keterangan", "Nilai"]
    assert ["Periode", "Agustus 2026"] in ringkasan.rows


def test_xls_ikut_jadi_tabel_terstruktur():
    assert tables.supports_tables("laporan_penjualan_lama.xls")
    result = tables.extract_tables(fixture("laporan_penjualan_lama.xls"), "laporan_penjualan_lama.xls")
    penjualan = [table for table in result if table.sheet == "Penjualan"][0]
    total = sum(int(row[5]) for row in penjualan.rows if row[5].isdigit())
    assert total == 2_911_000        # 540.000 + 625.000 + 1.050.000 + 336.000 + 360.000


# --------------------------------------------------------------------------- #
# .doc
# --------------------------------------------------------------------------- #


def test_doc_sederhana_semua_paragraf_terbaca():
    parts, meta = doc_binary.read_document(fixture("sop_cuti_lama.doc"))
    text = doc_binary.text_of_parts(parts)
    assert meta["kompleks"] is False
    assert "DOC2026MARK" in text
    assert "Kuota cuti tahunan" in text
    assert text.count("\n") >= 4, "setiap paragraf harus tetap terpisah"


def test_doc_piece_table_menggabungkan_potongan_ansi_dan_unicode():
    parts, meta = doc_binary.read_document(fixture("sop_cuti_potongan.doc"))
    text = doc_binary.text_of_parts(parts)
    assert meta["kompleks"] is True
    assert meta["piece_table"] is True
    assert "Bagian pertama" in text          # potongan satu byte per karakter
    assert "Bagian kedua" in text            # potongan UTF-16
    assert "é" in text and "ü" in text and "½" in text


def test_doc_menjadi_halaman_terpisah_per_bagian():
    parsed = parser.parse_document(fixture("sop_cuti_lama.doc"), "sop_cuti_lama.doc")
    assert parsed.parser == "doc-binary"
    assert parsed.text.strip().startswith("SOP Cuti 2026")


# --------------------------------------------------------------------------- #
# .ppt
# --------------------------------------------------------------------------- #


def test_ppt_semua_slide_dan_seluruh_teks():
    slides, meta = ppt_binary.read_presentation(fixture("presentasi_lama.ppt"))
    assert meta["slides"] == 2
    assert "PPT2026MARK" in slides[0]
    assert "Dibuat oleh Divisi Penjualan" in slides[0]      # teks kotak terakhir tidak hilang
    assert "Fokus produk Ayam Geprek" in slides[1]


def test_ppt_menjadi_satu_halaman_per_slide():
    parsed = parser.parse_document(fixture("presentasi_lama.ppt"), "presentasi_lama.ppt")
    assert parsed.parser == "ppt-binary"
    assert len(parsed.pages) == 2
    assert parsed.pages[1].text.startswith("Slide 2")


# --------------------------------------------------------------------------- #
# Pemilihan parser: isi berkas lebih dipercaya daripada nama
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name,expected", [
    ("sop_cuti_lama.doc", "doc_binary"),
    ("presentasi_lama.ppt", "ppt_binary"),
    ("laporan_penjualan_lama.xls", "xls_binary"),
])
def test_routing_berkas_biner_lama(name, expected):
    assert parser.parser_for(name, fixture(name), "") == expected


def test_doc_berisi_rtf_tetap_terbaca():
    content = fixture("lpj_rtf.doc")
    assert parser.parser_for("lpj_rtf.doc", content, "") == "rtf"
    parsed = parser.parse_document(content, "lpj_rtf.doc")
    assert "DOC2026MARK" in parsed.text


def test_doc_berisi_html_tetap_terbaca():
    content = fixture("catatan_html.doc")
    assert parser.parser_for("catatan_html.doc", content, "") == "html"
    assert "DOC2026MARK" in parser.parse_document(content, "catatan_html.doc").text


def test_ekstensi_salah_tetap_dikenali_dari_stream():
    """Berkas .xls yang isinya dokumen Word harus dibaca sesuai isinya, bukan namanya."""

    word = fixture("sop_cuti_lama.doc")
    assert parser.parser_for("salah_nama.xls", word, "") == "doc_binary"
    assert parser.parser_for("tanpa_ekstensi", word, "") == "doc_binary"
    assert parser.parser_for("salah_nama.doc", fixture("laporan_penjualan_lama.xls"), "") == "xls_binary"


def test_ole_tanpa_stream_dikenal_tidak_dipaksa():
    """Kontainer OLE yang bukan Word/Excel/PowerPoint tidak boleh diklaim sebagai salah satunya."""

    payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600
    assert parser.parser_for("misteri.bin", payload, "") in ("", "text")
