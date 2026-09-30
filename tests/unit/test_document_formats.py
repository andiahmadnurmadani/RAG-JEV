"""Format dokumen: katalog, ketersediaan, parser baru, dan pengikatan ke setelan.

Parser diuji dengan berkas yang benar-benar dibuat di sini (bukan mock): xlsx/pptx/odt/
epub adalah arsip zip yang isinya kita tulis sendiri, jadi testnya sekaligus membuktikan
penanganan format nyata tanpa pustaka tambahan.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.parsing import formats
from app.parsing.parser import parse_document


# --------------------------------------------------------------------------- #
# Katalog
# --------------------------------------------------------------------------- #


def test_catalog_is_consistent():
    keys = [spec.key for spec in formats.CATALOG]
    assert len(keys) == len(set(keys)), "kunci format harus unik"
    extensions = list(formats.EXTENSION_MAP)
    assert len(extensions) == sum(len(spec.extensions) for spec in formats.CATALOG)
    for spec in formats.CATALOG:
        assert spec.group in {"Dokumen", "Presentasi", "Spreadsheet", "Teks", "Gambar"}, spec
        assert spec.label and spec.parser
        for extension in spec.extensions:
            assert extension.startswith(".") and extension == extension.lower()


def test_default_extensions_only_reference_known_keys():
    for key in formats.DEFAULT_ENABLED_KEYS:
        assert key in formats.BY_KEY, key
    assert ".pdf" in formats.default_extensions()
    # gambar tidak pernah aktif tanpa OCR
    assert ".png" not in formats.default_extensions()


def test_describe_reports_availability_and_reason():
    rows = {row["key"]: row for row in formats.describe()}
    assert ".png" in rows["image"]["extensions"]
    for row in rows.values():
        assert isinstance(row["available"], bool)
        if not row["available"]:
            assert row["note"], f"{row['key']} tidak tersedia tapi tidak menjelaskan alasannya"
    assert rows["pdf"]["available"] is True


def test_enabled_extensions_follow_the_setting(settings):
    settings.upload_extensions = ".pdf,.xlsx,.txt"
    assert formats.enabled_extensions(settings) == [".pdf", ".txt", ".xlsx"]

    # kosong = default katalog
    settings.upload_extensions = ""
    assert ".docx" in formats.enabled_extensions(settings)

    # format yang belum didukung di mesin ini tidak pernah ikut
    settings.upload_extensions = ".png,.pdf"
    assert formats.enabled_extensions(settings) == [".pdf"]


def test_normalize_extensions_accepts_shorthand_and_rejects_unknown():
    assert formats.normalize_extensions(["xlsx", " .CSV ", "xlsx"]) == [".csv", ".xlsx"]
    with pytest.raises(ValueError):
        formats.normalize_extensions([".exe"])


# --------------------------------------------------------------------------- #
# Parser: berkas dibuat apa adanya di dalam test
# --------------------------------------------------------------------------- #


def _zip(entries: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _xlsx_bytes() -> bytes:
    shared = (
        '<?xml version="1.0"?><sst xmlns="x"><si><t>Nama</t></si>'
        '<si><t>Kuota Cuti 2026</t></si><si><t>Bagian</t></si><si><t>Keuangan</t></si></sst>'
    )
    sheet1 = (
        '<?xml version="1.0"?><worksheet><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>3</v></c>'
        '<c r="C2"><v>12</v></c></row>'
        "</sheetData></worksheet>"
    )
    sheet2 = (
        '<?xml version="1.0"?><worksheet><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Catatan rapat</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0"?><workbook><sheets>'
        '<sheet name="Kuota" sheetId="1" r:id="rId1"/>'
        '<sheet name="Catatan" sheetId="2" r:id="rId2"/>'
        "</sheets></workbook>"
    )
    rels = (
        '<?xml version="1.0"?><Relationships>'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>'
        "</Relationships>"
    )
    return _zip({
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": rels,
        "xl/sharedStrings.xml": shared,
        "xl/worksheets/sheet1.xml": sheet1,
        "xl/worksheets/sheet2.xml": sheet2,
    })


def _pptx_bytes() -> bytes:
    slide1 = (
        '<?xml version="1.0"?><p:sld xmlns:p="p" xmlns:a="a"><p:cSld><p:spTree>'
        "<a:p><a:r><a:t>Judul Rapat</a:t></a:r></a:p>"
        "<a:p><a:r><a:t>Pemeliharaan rutin</a:t></a:r></a:p>"
        "</p:spTree></p:cSld></p:sld>"
    )
    slide2 = (
        '<?xml version="1.0"?><p:sld xmlns:p="p" xmlns:a="a"><p:cSld><p:spTree>'
        "<a:p><a:r><a:t>Langkah berikutnya</a:t></a:r></a:p>"
        "</p:spTree></p:cSld></p:sld>"
    )
    notes = (
        '<?xml version="1.0"?><p:notes xmlns:p="p" xmlns:a="a">'
        "<a:p><a:r><a:t>Siapkan laporan bulanan</a:t></a:r></a:p></p:notes>"
    )
    return _zip({
        "ppt/slides/slide1.xml": slide1,
        "ppt/slides/slide2.xml": slide2,
        "ppt/notesSlides/notesSlide1.xml": notes,
    })


def _odt_bytes() -> bytes:
    content = (
        '<?xml version="1.0"?><office:document-content xmlns:text="text" xmlns:table="table">'
        "<text:h>Kebijakan Cuti</text:h>"
        "<text:p>Kuota cuti tahunan karyawan tetap.</text:p>"
        "<table:table><table:table-row>"
        "<table:table-cell><text:p>Bagian</text:p></table:table-cell>"
        "<table:table-cell><text:p>Kuota</text:p></table:table-cell>"
        "</table:table-row></table:table>"
        "</office:document-content>"
    )
    return _zip({"content.xml": content, "META-INF/manifest.xml": "<manifest/>"})


def _epub_bytes() -> bytes:
    container = (
        '<?xml version="1.0"?><container><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>'
    )
    opf = (
        '<?xml version="1.0"?><package><manifest>'
        '<item id="c2" href="bab2.xhtml"/>'
        '<item id="c1" href="bab1.xhtml"/>'
        "</manifest><spine>"
        '<itemref idref="c1"/><itemref idref="c2"/>'
        "</spine></package>"
    )
    bab1 = "<html><head><title>Bab Satu</title></head><body><p>Cuti tahunan dua belas hari.</p></body></html>"
    bab2 = "<html><body><p>Lembur dibayar setelah jam kerja.</p></body></html>"
    return _zip({
        "META-INF/container.xml": container,
        "OEBPS/content.opf": opf,
        "OEBPS/bab1.xhtml": bab1,
        "OEBPS/bab2.xhtml": bab2,
    })


def test_parse_xlsx_reads_every_sheet_with_shared_strings():
    parsed = parse_document(_xlsx_bytes(), "kuota.xlsx")
    assert parsed.parser == "xlsx-stdlib"
    assert len(parsed.pages) == 2
    assert "Kuota Cuti 2026" in parsed.pages[0].text
    assert "12" in parsed.pages[0].text
    assert "Catatan rapat" in parsed.pages[1].text
    assert parsed.language in {"id", "en"}


def test_parse_pptx_reads_slides_and_speaker_notes():
    parsed = parse_document(_pptx_bytes(), "rapat.pptx")
    assert parsed.parser == "pptx-stdlib"
    assert len(parsed.pages) == 2
    assert "Pemeliharaan rutin" in parsed.pages[0].text
    assert "Langkah berikutnya" in parsed.pages[1].text
    assert "Siapkan laporan bulanan" in parsed.pages[0].text


def test_parse_odf_reads_headings_paragraphs_and_table_cells():
    parsed = parse_document(_odt_bytes(), "kebijakan.odt")
    assert parsed.parser == "odf-stdlib"
    body = parsed.text
    assert "Kebijakan Cuti" in body
    assert "Kuota cuti tahunan" in body
    assert "Bagian | Kuota" in body


def test_parse_epub_follows_the_spine_order():
    parsed = parse_document(_epub_bytes(), "buku.epub")
    assert parsed.parser == "epub-stdlib"
    assert len(parsed.pages) == 2
    assert "dua belas hari" in parsed.pages[0].text  # bab1 dari spine, walau namanya sesudah bab2
    assert "Lembur" in parsed.pages[1].text


def test_parse_rtf_strips_control_words_and_decodes_escapes():
    rtf = (
        r"{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}"
        r"\f0\fs24 SOP Cuti 2026\par"
        r"Kuota cuti adalah 12 hari, biaya \'e9 00. \par}"
    ).encode("latin-1")
    parsed = parse_document(rtf, "sop.rtf")
    assert parsed.parser == "rtf-stdlib"
    assert "SOP Cuti 2026" in parsed.text
    assert "Arial" not in parsed.text, "tabel font tidak boleh masuk teks"
    assert "\rtf1" not in parsed.text
    assert "é" in parsed.text


def test_parse_csv_detects_delimiter_and_labels_rows():
    content = "nama;bagian;kuota\nAndi;Keuangan;12\nBudi;Operasi;12\n".encode()
    parsed = parse_document(content, "kuota.csv")
    assert parsed.parser == "csv-stdlib"
    assert "baris 2: Andi | Keuangan | 12" in parsed.text


def test_parse_tsv_works_too():
    parsed = parse_document(b"kode\tarti\nA1\tcuti\n", "kamus.tsv")
    assert "cuti" in parsed.text


def test_parse_xml_removes_tags_but_keeps_text():
    parsed = parse_document(b"<sop><judul>Cuti</judul><isi>12 hari</isi></sop>", "sop.xml")
    assert parsed.parser == "xml"
    assert "Cuti" in parsed.text and "12 hari" in parsed.text
    assert "<judul>" not in parsed.text


def test_parse_text_handles_non_utf8_files():
    content = "Ringkasan: kuota cuti 12 hari, caf\xe9 buka 08.00".encode("latin-1")
    parsed = parse_document(content, "catatan.txt")
    assert "kuota cuti 12 hari" in parsed.text
    assert "caf" in parsed.text


def test_parse_jsonl_is_readable_as_text():
    payload = b'{"nama": "Andi", "kuota": 12}\n{"nama": "Budi", "kuota": 12}\n'
    parsed = parse_document(payload, "data.jsonl")
    assert "Andi" in parsed.text and "Budi" in parsed.text


def test_yaml_and_log_files_are_readable():
    assert "cuti: 12" in parse_document(b"cuti: 12\n", "kebijakan.yaml").text
    assert "ERROR" in parse_document(b"2026-09-30 ERROR gagal\n", "app.log").text


def test_image_without_ocr_says_so_instead_of_silently_failing():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    if formats.availability(formats.BY_KEY["image"]):
        pytest.skip("OCR terpasang di mesin ini; jalur OCR diuji terpisah")
    with pytest.raises(AppError) as excinfo:
        parse_document(png, "scan.png")
    assert excinfo.value.code in {"UNSUPPORTED_MEDIA_TYPE", "INDEXING_FAILED"}
    assert "OCR" in str(excinfo.value)


def test_unknown_extension_is_refused_by_the_router():
    with pytest.raises(AppError) as excinfo:
        parse_document(b"MZ\x90\x00", "payload.exe")
    assert excinfo.value.code == "UNSUPPORTED_MEDIA_TYPE"


def test_corrupt_office_file_fails_with_a_readable_message():
    with pytest.raises(AppError) as excinfo:
        parse_document(b"bukan zip sama sekali", "rusak.xlsx")
    assert excinfo.value.code == "INDEXING_FAILED"
    assert "xlsx" in str(excinfo.value).lower()


# --------------------------------------------------------------------------- #
# Ciri berkas nyata: urutan atribut XML tidak seragam, kata kontrol RTF bisa menempel
# --------------------------------------------------------------------------- #


def _realistic_xlsx_bytes() -> bytes:
    """xlsx seperti keluaran openpyxl: Target sebelum Id, dan r:id di elemen sheet."""
    shared = '<?xml version="1.0"?><sst xmlns="x"><si><t>Kuota Cuti</t></si><si><t>12</t></si></sst>'
    sheet = (
        '<?xml version="1.0"?><worksheet><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        "</sheetData></worksheet>"
    )
    workbook = (
        '<?xml version="1.0"?><workbook><sheets>'
        '<sheet name="Kuota" sheetId="1" r:id="rId1"/>'
        "</sheets></workbook>"
    )
    rels = (
        '<?xml version="1.0"?><Relationships>'
        '<Relationship Type="http://x/worksheet" Target="/xl/worksheets/sheet1.xml" Id="rId1"/>'
        "</Relationships>"
    )
    return _zip({
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": rels,
        "xl/sharedStrings.xml": shared,
        "xl/worksheets/sheet1.xml": sheet,
    })


def test_xlsx_reads_sheet_names_even_when_attributes_are_reordered():
    parsed = parse_document(_realistic_xlsx_bytes(), "kuota.xlsx")
    assert "Lembar: Kuota" in parsed.pages[0].text, parsed.pages[0].text
    assert "baris 1: A=Kuota Cuti" in parsed.pages[0].text


def test_epub_manifest_works_when_href_comes_before_id_and_nav_is_skipped():
    container = (
        '<?xml version="1.0"?><container><rootfiles>'
        '<rootfile full-path="EPUB/content.opf"/></rootfiles></container>'
    )
    opf = (
        '<?xml version="1.0"?><package><manifest>'
        '<item href="bab1.xhtml" id="chapter_0" media-type="application/xhtml+xml"/>'
        '<item href="nav.xhtml" id="nav" media-type="application/xhtml+xml" properties="nav"/>'
        "</manifest><spine>"
        '<itemref idref="nav"/><itemref idref="chapter_0"/>'
        "</spine></package>"
    )
    bab = "<html><body><p>Cuti tahunan dua belas hari.</p></body></html>"
    nav = "<html><body><a href='bab1.xhtml'>Bab 1</a></body></html>"
    parsed = parse_document(
        _zip({"META-INF/container.xml": container, "EPUB/content.opf": opf,
              "EPUB/bab1.xhtml": bab, "EPUB/nav.xhtml": nav}),
        "buku.epub",
    )
    assert len(parsed.pages) == 1, [p.text for p in parsed.pages]
    assert "dua belas hari" in parsed.pages[0].text


def test_rtf_control_word_glued_to_text_keeps_the_words_apart():
    """Sebagian perkakas menulis par tanpa pembatas: teksnya tidak boleh hilang atau menempel."""
    backslash = chr(92)
    rtf = ("{" + backslash + "rtf1" + backslash + "ansi" + backslash + "deff0" + backslash + "f0" +
           backslash + "fs24 Judul" + backslash + "parKuota cuti adalah 12 hari." +
           backslash + "parBiaya kafe ditanggung.}").encode()
    parsed = parse_document(rtf, "sop.rtf")
    assert parsed.text.splitlines()[:3] == ["Judul", "Kuota cuti adalah 12 hari.", "Biaya kafe ditanggung."]


def test_rtf_keeps_dashes_and_quotes_from_control_symbols():
    backslash = chr(92)
    rtf = ("{" + backslash + "rtf1" + backslash + "ansi Kode " + backslash + "endash" + backslash +
           " 2026 " + backslash + "ldblquote cuti" + backslash + "rdblquote " + backslash +
           "bullet" + backslash + " lampiran}").encode()
    parsed = parse_document(rtf, "sop.rtf")
    assert chr(92) + "u2013" not in parsed.text
    assert chr(0x2013) in parsed.text and chr(0x201C) in parsed.text and chr(0x2022) in parsed.text


def test_odf_table_cells_do_not_leave_empty_separators():
    odt = _odt_bytes()
    parsed = parse_document(odt, "kebijakan.odt")
    assert "|  |" not in parsed.text
    assert "Bagian | Kuota" in parsed.text

