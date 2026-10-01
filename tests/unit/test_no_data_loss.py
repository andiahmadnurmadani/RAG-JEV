"""Jaminan "tidak ada bagian knowledge yang hilang" saat berkas diunggah.

Setiap tes di sini menaruh data di tempat yang dulu terlewat, lalu memastikan datanya
benar-benar sampai ke teks knowledge atau ke baris tabel:

* docx: header/footer, catatan kaki/akhir, komentar, properti dokumen, dan sel tabel
  (dulu hanya ``word/document.xml`` yang dibaca dan sel tabel menyatu tanpa pemisah);
* xlsx: sel bertipe tanggal (dulu tersimpan sebagai serial 46075), header/footer cetak,
  komentar sel, dan teks di objek gambar;
* ods: header/footer di ``styles.xml`` dan sel angka/tanggal tanpa teks;
* pptx: catatan pembicara, master, dan tata letak (dulu hanya slide);
* eml: isi surat + lampiran;
* tabel: baris judul lembar tidak lagi memotong seluruh kolom (dulu lebar tabel dikunci
  oleh baris judul sehingga kolom B..F hilang), lebar diambil dari seluruh baris, dan
  pemotongan baris dicatat, bukan disembunyikan.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.parsing import parser, sheet_dates, tables
from app.parsing.parser import ParsedDocument, ParsedPage
from app.rag.chunker import chunk_document, estimate_tokens

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _zip(members: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body if isinstance(body, bytes) else body.encode("utf-8"))
    return buffer.getvalue()


DOCX = _zip({
    "word/document.xml": f'''<?xml version="1.0"?><w:document xmlns:w="{W}">
      <w:body>
        <w:p><w:r><w:t>Judul rapat tahunan</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Nama</w:t></w:r></w:p></w:tc>
                   <w:tc><w:p><w:r><w:t>Nilai</w:t></w:r></w:p></w:tc></w:tr>
              <w:tr><w:tc><w:p><w:r><w:t>Budi</w:t></w:r></w:p></w:tc>
                   <w:tc><w:p><w:r><w:t>14830000</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        <w:p><w:r><w:instrText> PAGE \\* MERGEFORMAT </w:instrText></w:r></w:p>
      </w:body></w:document>''',
    "word/header1.xml": f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>PT Contoh Sejahtera</w:t></w:r></w:p></w:hdr>',
    "word/footer1.xml": f'<w:ftr xmlns:w="{W}"><w:p><w:r><w:t>Dokumen internal nomor 77</w:t></w:r></w:p></w:ftr>',
    "word/footnotes.xml": f'<w:footnotes xmlns:w="{W}"><w:p><w:r><w:t>Catatan kaki: pajak belum dihitung</w:t></w:r></w:p></w:footnotes>',
    "word/comments.xml": f'<w:comments xmlns:w="{W}"><w:p><w:r><w:t>Komentar: angka perlu dicek ulang</w:t></w:r></w:p></w:comments>',
    "docProps/core.xml": "<cp:coreProperties><dc:title>Rapat Tahunan 2026</dc:title></cp:coreProperties>",
})

XLSX = _zip({
    "xl/workbook.xml": f'''<workbook xmlns="{S}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
        <workbookPr/><sheets><sheet name="Penjualan" sheetId="1" r:id="rId1"/></sheets></workbook>''',
    "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
    "xl/styles.xml": f'''<styleSheet xmlns="{S}">
        <numFmts count="1"><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/></numFmts>
        <cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs></styleSheet>''',
    "xl/worksheets/sheet1.xml": f'''<worksheet xmlns="{S}"><sheetData>
        <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
        <row r="2"><c r="A2" s="1"><v>46075</v></c><c r="B2"><v>14830000</v></c></row>
      </sheetData>
      <headerFooter><oddHeader>&amp;C Laporan Penjualan</oddHeader></headerFooter></worksheet>''',
    "xl/worksheets/_rels/sheet1.xml.rels": '<Relationships><Relationship Id="rId1" Target="../comments1.xml"/></Relationships>',
    "xl/comments1.xml": f'<comments xmlns="{S}"><commentList><comment ref="B2"><text><t>Komentar sel: sudah diaudit</t></text></comment></commentList></comments>',
    "xl/sharedStrings.xml": f'<sst xmlns="{S}"><si><t>Tanggal</t></si><si><t>Total</t></si></sst>',
})

ODS = _zip({
    "content.xml": '''<?xml version="1.0"?><office:document-content
        xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
        xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
        xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
      <office:body><office:spreadsheet><table:table table:name="Stok">
        <table:table-row>
          <table:table-cell office:value-type="string"><text:p>Gula</text:p></table:table-cell>
          <table:table-cell office:value-type="float" office:value="350"/>
          <table:table-cell office:value-type="date" office:date-value="2026-08-02"/>
        </table:table-row>
      </table:table></office:spreadsheet></office:body></office:document-content>''',
    "styles.xml": '''<?xml version="1.0"?><office:document-styles
        xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
        xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
        xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0">
      <office:master-styles><style:master-page style:name="Standard">
        <style:header><text:p>Gudang Pusat Bandung</text:p></style:header>
      </style:master-page></office:master-styles></office:document-styles>''',
    "meta.xml": "<office:document-meta><meta:title>Stok Gudang</meta:title></office:document-meta>",
})

PPTX = _zip({
    "ppt/slides/slide1.xml": '''<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
        <a:p><a:r><a:t>Ringkasan kuartal</a:t></a:r></a:p></p:sld>''',
    "ppt/notesSlides/notesSlide1.xml": '''<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
        <a:p><a:r><a:t>Catatan: tekankan pertumbuhan 20 persen</a:t></a:r></a:p></p:notes>''',
    "ppt/slideMasters/slideMaster1.xml": '''<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
        <a:p><a:r><a:t>PT Contoh Sejahtera - Divisi Penjualan</a:t></a:r></a:p></p:sldMaster>''',
})

EML = (b"From: budi@contoh.co.id\r\nTo: tim@contoh.co.id\r\nSubject: Laporan September\r\n"
       b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=BATAS\r\n\r\n"
       b"--BATAS\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
       b"Total penjualan 14.830.000 rupiah.\r\n"
       b"--BATAS\r\nContent-Type: application/pdf; name=lampiran.pdf\r\n"
       b"Content-Disposition: attachment; filename=lampiran.pdf\r\n\r\nPDF\r\n--BATAS--\r\n")


# --------------------------------------------------------------------------- #
# docx
# --------------------------------------------------------------------------- #


def test_docx_membaca_header_footer_catatan_komentar_dan_properti():
    parsed = parser.parse_document(DOCX, "rapat.docx")
    text = parsed.text
    assert "PT Contoh Sejahtera" in text
    assert "Dokumen internal nomor 77" in text
    assert "pajak belum dihitung" in text
    assert "angka perlu dicek ulang" in text
    assert "Rapat Tahunan 2026" in text


def test_docx_sel_tabel_tidak_menyatu():
    text = parser.parse_document(DOCX, "rapat.docx").text
    assert "Nama | Nilai" in text
    assert "Budi | 14830000" in text


def test_docx_kode_field_tidak_ikut_jadi_teks():
    assert "MERGEFORMAT" not in parser.parse_document(DOCX, "rapat.docx").text


# --------------------------------------------------------------------------- #
# xlsx
# --------------------------------------------------------------------------- #


def test_xlsx_tanggal_ditulis_sebagai_tanggal_bukan_serial():
    text = parser.parse_document(XLSX, "penjualan.xlsx").text
    assert "2026-02-22" in text
    assert "46075" not in text


def test_xlsx_komentar_dan_header_cetak_ikut_terbaca():
    text = parser.parse_document(XLSX, "penjualan.xlsx").text
    assert "sudah diaudit" in text
    assert "Laporan Penjualan" in text


def test_tabel_xlsx_tanggal_bukan_kolom_angka():
    sheet = tables.extract_tables(XLSX, "penjualan.xlsx")[0]
    assert sheet.headers == ["Tanggal", "Total"]
    assert sheet.rows[0][0] == "2026-02-22"
    columns = {column["name"]: column for column in sheet.schema()["columns"]}
    assert columns["Tanggal"]["numeric"] is False


# --------------------------------------------------------------------------- #
# ods
# --------------------------------------------------------------------------- #


def test_ods_nilai_sel_tanpa_teks_tidak_hilang():
    text = parser.parse_document(ODS, "stok.ods").text
    assert "350" in text                       # office:value pada sel angka
    assert "2026-08-02" in text                # office:date-value pada sel tanggal


def test_ods_header_dari_styles_dan_meta():
    text = parser.parse_document(ODS, "stok.ods").text
    assert "Gudang Pusat Bandung" in text
    assert "Stok Gudang" in text


# --------------------------------------------------------------------------- #
# pptx / eml
# --------------------------------------------------------------------------- #


def test_pptx_catatan_master_dan_slide_terbaca():
    text = parser.parse_document(PPTX, "kuartal.pptx").text
    assert "Ringkasan kuartal" in text
    assert "tekankan pertumbuhan 20 persen" in text
    assert "Divisi Penjualan" in text


def test_eml_isi_dan_lampiran():
    text = parser.parse_document(EML, "surat.eml").text
    assert "Total penjualan 14.830.000 rupiah" in text
    assert "Lampiran: lampiran.pdf" in text


# --------------------------------------------------------------------------- #
# Tabel: baris judul dan lebar kolom
# --------------------------------------------------------------------------- #


def test_baris_judul_lembar_tidak_menghapus_kolom():
    rows = [
        ["RINGKASAN DATA PENJUALAN"],
        ["Bulan", "Penjualan", "Laba Kotor"],
        ["Januari", "8285000", "1957544"],
        ["Februari", "8500000", "1352136"],
    ]
    table = tables._build_table("Ringkasan", rows, ".xlsx")
    assert table.headers == ["Bulan", "Penjualan", "Laba Kotor"]
    assert table.rows[0] == ["Januari", "8285000", "1957544"]
    assert any("judul lembar" in note for note in table.notes)


def test_lebar_tabel_diambil_dari_seluruh_baris():
    rows = [
        ["Nama", "Nilai"],
        ["Budi", "10"],
        ["Siti", "20", "catatan tambahan di kolom C"],
    ]
    table = tables._build_table("Data", rows, ".xlsx")
    assert len(table.headers) == 3
    assert table.rows[1][2] == "catatan tambahan di kolom C"
    assert table.rows[0][2] == ""      # baris lain tetap sejajar


def test_pemotongan_baris_dicatat_bukan_disembunyikan(monkeypatch):
    monkeypatch.setattr(tables, "MAX_TABLE_ROWS", 2)
    rows = [["Nama", "Nilai"]] + [[f"baris{i}", str(i)] for i in range(10)]
    table = tables._build_table("Besar", rows, ".csv")
    assert table.truncated is True
    assert len(table.rows) == 2
    assert any("hanya 2 baris" in note for note in table.notes)


def test_kolom_tanpa_isi_dicatat_namanya():
    rows = [["Nama", "Kosong", "Nilai"], ["Budi", "", "10"], ["Siti", "", "20"]]
    table = tables._build_table("Data", rows, ".csv")
    assert table.headers == ["Nama", "Nilai"]
    assert any("Kosong" in note for note in table.notes)


# --------------------------------------------------------------------------- #
# Penerjemah tanggal
# --------------------------------------------------------------------------- #


def test_gaya_tanggal_mengubah_serial_menjadi_tanggal():
    styles = sheet_dates.parse_styles(
        '<styleSheet><numFmts><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/></numFmts>'
        '<cellXfs><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs></styleSheet>')
    assert styles.is_date_style(1) is True
    assert styles.is_date_style(0) is False
    assert sheet_dates.styled_value("46075", 1, styles) == "2026-02-22"
    assert sheet_dates.styled_value("14830000", 0, styles) == "14830000"


def test_date1904_dihormati():
    styles = sheet_dates.WorkbookStyles({0}, {}, date1904=True)
    assert sheet_dates.styled_value("0", 0, styles) == "1904-01-01"


def test_gaya_tanggal_bawaan_microsoft_dikenali():
    styles = sheet_dates.parse_styles(
        '<styleSheet><cellXfs><xf numFmtId="14"/><xf numFmtId="0"/></cellXfs></styleSheet>')
    assert styles.is_date_style(0) is True
    assert styles.is_date_style(1) is False


@pytest.mark.parametrize("header", ["Tanggal", "Tgl Transaksi", "Date", "Waktu Mulai"])
def test_nama_kolom_tanggal_dikenali(header):
    assert sheet_dates.looks_like_date_column(header, ["2026-08-02", "2026-08-03"]) is True


def test_kolom_bukan_tanggal_tidak_ditebak():
    assert sheet_dates.looks_like_date_column("Produk", ["Laptop", "Mouse"]) is False
    assert sheet_dates.looks_like_date_column("Tanggal", ["PRD-001", "PRD-002"]) is False


# --------------------------------------------------------------------------- #
# Catatan tabel (judul lembar, pemotongan) tidak boleh hilang di penyimpanan
# --------------------------------------------------------------------------- #


def test_notes_tabel_bertahan_di_penyimpanan(tmp_path):
    from app.tables.store import TableStore
    from app.parsing.tables import TableData

    store = TableStore(str(tmp_path / "tabel.sqlite"))
    tabel = TableData(sheet="Ringkasan", headers=["A", "B"], rows=[["1", "2"]],
                      truncated=True, notes=["judul lembar: RINGKASAN DATA", "hanya 1 baris dipakai"])
    store.replace_document(organization_id="org_a", knowledge_base_id="kb", document_id="doc-1",
                           document_name="contoh.xlsx", tables=[tabel])
    tersimpan = store.list_tables(organization_id="org_a", knowledge_base_id="kb")[0]
    assert tersimpan.truncated is True
    assert "judul lembar: RINGKASAN DATA" in tersimpan.notes
    assert tersimpan.as_dict()["notes"] == tersimpan.notes


def test_kalimat_cakupan_jujur_untuk_tabel_terpotong():
    from app.parsing.tables import TableData, scope_note

    bersih = TableData(sheet="S", headers=["A"], rows=[["1"]])
    assert scope_note(bersih) == ""

    terpotong = TableData(sheet="S", headers=["A"], rows=[["1"]], truncated=True,
                          notes=["judul lembar: RINGKASAN"])
    kalimat = scope_note(terpotong)
    assert "dipotong" in kalimat and "judul lembar" in kalimat


def test_berkas_berhenti_di_batas_baris_dilaporkan(monkeypatch):
    """Berkas yang berhenti tepat di batas baris dulu tampak utuh padahal sisanya tidak dibaca."""

    monkeypatch.setattr(tables, "MAX_TABLE_ROWS", 5)
    isi = ("Barang,Qty\n" + "\n".join(f"barang-{i},1" for i in range(20))).encode()
    tabel = tables.extract_tables(isi, "besar.csv")[0]
    assert tabel.truncated is True
    assert len(tabel.rows) == 5
    assert any("lebih dari 5 baris" in note for note in tabel.notes)
    assert "dipotong" in tables.scope_note(tabel)


def test_baris_lebih_banyak_dari_batas_ikut_dipotong(monkeypatch):
    monkeypatch.setattr(tables, "MAX_TABLE_ROWS", 3)
    baris = [["Nama", "Nilai"]] + [[f"b{i}", "1"] for i in range(6)]
    tabel = tables._build_table("S", baris, ".xlsx")
    assert tabel.truncated is True
    assert len(tabel.rows) == 3
    assert any("hanya 3 baris pertama dari 6 baris" in note for note in tabel.notes)


def test_tabel_besar_dipotong_dengan_baris_kepala_diulang():
    """Tabel besar tidak boleh jadi satu chunk raksasa (isi jadi tak bisa dicari)."""

    header = "Barang | Qty | Harga"
    baris = [f"barang-{i} | 1 | 1000" for i in range(400)]
    table = header + "\n" + "\n".join(baris)
    parsed = ParsedDocument(document_name="besar.csv", pages=[ParsedPage(page=1, text=table)],
                            parser="csv-stdlib")
    chunks = chunk_document(parsed, document_id="doc_tabel", chunk_size=200, chunk_overlap=0)
    assert len(chunks) > 3, f"tabel 400 baris hanya jadi {len(chunks)} chunk"
    assert all(chunk.content.splitlines()[0] == header for chunk in chunks), "baris kepala hilang"
    assert all(estimate_tokens(chunk.content) < 400 for chunk in chunks)
    gabungan_baris = [line for chunk in chunks for line in chunk.content.splitlines() if line.startswith("barang-")]
    assert len(gabungan_baris) == len(baris), "ada baris tabel yang hilang saat dipotong"


def test_paragraf_biasa_tetap_dipotong_per_kalimat():
    teks = " ".join(f"Kalimat nomor {i} berisi penjelasan cukup panjang." for i in range(120))
    parsed = ParsedDocument(document_name="teks.txt", pages=[ParsedPage(page=1, text=teks)], parser="text")
    chunks = chunk_document(parsed, document_id="doc_teks", chunk_size=150, chunk_overlap=0)
    assert len(chunks) > 2
    assert all(estimate_tokens(chunk.content) < 300 for chunk in chunks)


def test_chunk_tabel_selalu_diawali_baris_kepala_setelah_tumpang_tindih():
    header = "Barang | Qty | Harga"
    baris = [f"barang-{i} | 1 | 1000" for i in range(400)]
    parsed = ParsedDocument(document_name="besar.csv",
                            pages=[ParsedPage(page=1, text=header + "\n" + "\n".join(baris))],
                            parser="csv-stdlib")
    chunks = chunk_document(parsed, document_id="doc_tabel", chunk_size=200, chunk_overlap=60)
    assert len(chunks) > 3
    for chunk in chunks:
        assert chunk.content.splitlines()[0] == header, "baris kepala tidak di baris pertama"
