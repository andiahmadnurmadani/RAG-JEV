"""Document parsing → page/section-aware text (PRD 10, 34)."""

from __future__ import annotations

import email
import email.policy
import fnmatch
import html
import io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.errors import AppError
from app.parsing import doc_binary, ppt_binary, sheet_dates, xls_binary
from app.parsing.formats import EXTENSION_MAP
from app.parsing.sanitize import clean_text, looks_like_binary_garbage

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\u00a0]+")
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Awal berkas RTF: { \ r t f  -- ditulis sebagai kode byte supaya tidak jadi karakter kontrol.
RTF_HEADER = bytes([0x7B, 0x5C, 0x72, 0x74, 0x66])


@dataclass
class ParsedPage:
    page: int
    text: str
    # Alamat asal halaman ini bila dokumennya datang dari web (crawl). Dipakai supaya sitasi
    # menunjuk halaman web yang tepat, bukan hanya nomor halaman.
    source_url: str = ""
    title: str = ""


@dataclass
class ParsedDocument:
    document_name: str
    pages: List[ParsedPage] = field(default_factory=list)
    language: str = "id"
    parser: str = "plain"

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text.strip())


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_pdf(content: bytes, document_name: str) -> ParsedDocument:
    """Teks PDF, dengan tabel dirender sebagai baris ``| a | b |`` bila bisa.

    Dua mesin dipakai berurutan. PyMuPDF dulu karena urutan bacanya mengikuti tata letak
    (kolom tidak tertukar) dan ia bisa mengenali tabel; tanpa itu isi tabel datang sebagai
    baris berspasi yang tidak bisa dipotong per baris - potongan jadi terbelah di tengah baris
    dan baris tabel inti tidak bisa ditemukan kembali. pypdf tetap jadi cadangan supaya
    pemasangan tanpa PyMuPDF tidak kehilangan apa pun.
    """
    pages = _pdf_pages_pymupdf(content)
    parser = "pymupdf"
    if pages is None:
        pages = _pdf_pages_pypdf(content)
        parser = "pypdf"
    if not any(page.text for page in pages):
        raise AppError("INDEXING_FAILED", "PDF contains no extractable text (scanned file?)")
    return ParsedDocument(document_name=document_name, pages=pages, parser=parser)


def _pdf_pages_pymupdf(content: bytes) -> Optional[List[ParsedPage]]:
    """Halaman-halaman PDF lewat PyMuPDF; ``None`` bila pustakanya tidak ada."""

    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24 (nama modul baru)
    except ImportError:
        try:
            import fitz  # PyMuPDF lama
        except ImportError:  # pragma: no cover - bergantung pemasangan
            return None

    pages: List[ParsedPage] = []
    try:
        with fitz.open(stream=content, filetype="pdf") as document:
            for number, page in enumerate(document, start=1):
                try:
                    text = _pymupdf_page_text(page)
                except Exception:  # noqa: BLE001 — satu halaman rusak tidak boleh mematikan berkas
                    text = ""
                pages.append(ParsedPage(page=number, text=_normalize(text)))
    except Exception:  # noqa: BLE001 - berkas tidak terbaca PyMuPDF: biarkan pypdf mencoba
        return None
    return pages or None


def _pymupdf_page_text(page: Any) -> str:
    """Teks satu halaman, dengan area tabel diganti baris berformat ``| sel | sel |``.

    Blok teks yang berada DI DALAM area tabel dibuang supaya isinya tidak muncul dua kali
    (sekali sebagai prosa, sekali sebagai baris tabel) - duplikat menggandakan token dan
    membuat pencarian mengembalikan dua potongan yang isinya sama.
    """

    tables: List[Any] = []
    try:
        found = page.find_tables()
        tables = list(getattr(found, "tables", []) or [])
    except Exception:  # noqa: BLE001 - deteksi tabel bersifat bonus
        tables = []
    plain = page.get_text("text", sort=True) or ""
    if not tables:
        return plain

    boxes = [tuple(getattr(table, "bbox", ()) or ()) for table in tables]
    pieces: List[tuple[float, float, str]] = []
    for block in page.get_text("blocks", sort=True) or []:
        try:
            x0, y0, x1, y1, text = block[0], block[1], block[2], block[3], block[4]
        except (IndexError, TypeError):  # pragma: no cover - bentuk blok tak terduga
            continue
        if not str(text or "").strip():
            continue
        centre = ((float(x0) + float(x1)) / 2.0, (float(y0) + float(y1)) / 2.0)
        if any(_point_in_box(centre, box) for box in boxes if len(box) == 4):
            continue
        pieces.append((float(y0), float(x0), str(text).strip()))

    for table in tables:
        rows = []
        for row in getattr(table, "extract", lambda: [])() or []:
            cells = [" ".join(str(cell or "").split()) for cell in row]
            if any(cells):
                rows.append("| " + " | ".join(cells) + " |")
        if not rows:
            continue
        box = tuple(getattr(table, "bbox", ()) or ())
        y = float(box[1]) if len(box) == 4 else 0.0
        x = float(box[0]) if len(box) == 4 else 0.0
        pieces.append((y, x, "\n".join(rows)))

    pieces.sort(key=lambda item: (round(item[0], 1), item[1]))
    rendered = "\n\n".join(text for _, _, text in pieces)
    return rendered or plain


def _point_in_box(point: tuple[float, float], box: tuple[float, float, float, float]) -> bool:
    x, y = point
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _pdf_pages_pypdf(content: bytes) -> List[ParsedPage]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise AppError("INDEXING_FAILED", "pypdf is not installed") from exc
    reader = PdfReader(io.BytesIO(content))
    pages: List[ParsedPage] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            pages.append(ParsedPage(page=number, text=_normalize(page.extract_text() or "")))
        except Exception:  # noqa: BLE001 — a single unreadable page must not kill the document
            pages.append(ParsedPage(page=number, text=""))
    return pages


def _word_part_text(xml: str) -> str:
    """Teks satu bagian Word (document/header/footer/catatan): tabel tetap terpisah jelas."""

    xml = re.sub(r"(?is)<w:instrText[^>]*>.*?</w:instrText>", "", xml)   # kode field, bukan isi
    xml = re.sub(r"(?is)<w:delText[^>]*>.*?</w:delText>", "", xml)       # teks yang dihapus (track changes)
    xml = re.sub(r"(?is)<w:tc[^>]*>", "", xml)
    # Penanda sementara: batas sel harus menang atas batas paragraf di dalam sel,
    # supaya satu baris tabel tidak pecah jadi beberapa baris teks.
    xml = re.sub(r"(?is)</w:tc>", "\x01", xml)
    xml = re.sub(r"(?is)</w:tr>", "\n", xml)
    xml = re.sub(r"(?is)</w:p>", "\n", xml)
    xml = re.sub(r"(?is)<w:(tab|ptab)[^>]*/>", "\t", xml)
    xml = re.sub(r"(?is)<w:br[^>]*/>", "\n", xml)
    text = html.unescape(TAG_RE.sub("", xml)).replace("\x01", "|")
    text = re.sub(r"\s*\|\s*", " | ", text)
    text = re.sub(r"(?m)[ \t]*\|[ \t]*$", "", text)
    return _normalize(text)


def _docx_extra_parts(archive: "zipfile.ZipFile", names: set) -> List[tuple]:
    """Header, footer, catatan kaki/akhir, komentar, dan properti dokumen.

    Semuanya ikut dibaca: isi header/footer dan catatan kaki sering memuat informasi
    (nomor dokumen, revisi, syarat) yang tidak ada di badan dokumen.
    """

    blocks: List[tuple] = []
    for pattern, label in (
        ("word/header*.xml", "Header"),
        ("word/footer*.xml", "Footer"),
        ("word/footnotes.xml", "Catatan kaki"),
        ("word/endnotes.xml", "Catatan akhir"),
        ("word/comments.xml", "Komentar"),
    ):
        for name in sorted(member for member in names if fnmatch.fnmatch(member, pattern)):
            text = _word_part_text(archive.read(name).decode("utf-8", "ignore"))
            if text:
                suffix = Path(name).stem.replace("header", "").replace("footer", "")
                blocks.append((f"{label}{suffix}", text))
    if "docProps/core.xml" in names:
        raw = archive.read("docProps/core.xml").decode("utf-8", "ignore")
        meta = []
        for tag in ("title", "subject", "creator", "description", "keywords", "lastModifiedBy"):
            found = re.search(rf"(?is)<(?:dc:|cp:|dcterms:)?{tag}[^>]*>(.*?)</", raw)
            if found and found.group(1).strip():
                meta.append(f"{tag}: {html.unescape(TAG_RE.sub('', found.group(1))).strip()}")
        if meta:
            blocks.append(("Properti dokumen", "\n".join(meta)))
    return blocks


def parse_docx(content: bytes, document_name: str) -> ParsedDocument:
    """Baca seluruh bagian .docx (badan, header/footer, catatan, komentar) tanpa python-docx."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            xml = archive.read("word/document.xml").decode("utf-8", "ignore")
            extras = _docx_extra_parts(archive, names)
    except Exception as exc:  # noqa: BLE001
        raise AppError("INDEXING_FAILED", f"Unreadable .docx: {exc}") from exc

    parts = [("Teks utama", _word_part_text(xml)), *extras]
    pages: List[ParsedPage] = []
    for label, text in parts:
        body = text.strip()
        if not body:
            continue
        pages.append(ParsedPage(page=len(pages) + 1, text=body if label == "Teks utama" else f"[{label}]\n{body}"))
    if not any(page.text.strip() for page in pages):
        raise AppError("INDEXING_FAILED", "Word document has no readable text")
    return ParsedDocument(document_name=document_name, pages=pages, parser="docx-xml")


def parse_html(content: bytes, document_name: str) -> ParsedDocument:
    text = _html_to_text(_decode_text(content))
    title = _document_title(_decode_text(content), document_name)
    if title and not text.lower().startswith(title.lower()[:40]):
        text = f"{title}\n{text}"  # judul hanya ditambahkan bila belum ada di badan teks
    if not text.strip():
        raise AppError("INDEXING_FAILED", "HTML file has no readable text")
    return ParsedDocument(document_name=document_name, pages=[ParsedPage(page=1, text=_normalize(text))], parser="html")


def _decode_text(content: bytes) -> str:
    """Decode bytes to text, tolerating non-UTF-8 files (a real problem for .txt/.csv/.log)."""
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(content).best()
        if best is not None:
            return str(best)
    except Exception:  # noqa: BLE001 - detection is best effort
        pass
    return content.decode("utf-8", "ignore")


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|section|article|li|h[1-6]|tr|table|blockquote|dd|dt|figcaption|pre)>", "\n", raw)
    raw = re.sub(r"(?i)</t[dh]>", " | ", raw)
    raw = re.sub(r"(?i)<t[dh]\b[^>]*>", "", raw)
    text = _normalize(html.unescape(TAG_RE.sub("", raw)))
    return re.sub(r"(\s*\|\s*)+", " | ", text).strip(" |").strip()


def _document_title(raw: str, fallback: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    if match:
        title = _normalize(html.unescape(TAG_RE.sub("", match.group(1))))
        if title:
            return title[:120]
    return fallback


def _tag_attributes(tag: str) -> Dict[str, str]:
    """Ambil semua atribut satu tag tanpa peduli urutannya (XML nyata tidak seragam)."""
    return {name.lower(): html.unescape(value) for name, value in re.findall(r'([A-Za-z_:][-\w:.]*)\s*=\s*"([^"]*)"', tag)}


def _tag_value(tag: str, name: str) -> str:
    return _tag_attributes(tag).get(name.lower(), "")

def _xlsx_sheet_rows(xml: str, shared: List[str],
                     styles: Optional[sheet_dates.WorkbookStyles] = None) -> List[str]:
    rows: List[str] = []
    for row_match in re.finditer(r"(?is)<row\b([^>]*)>(.*?)</row>", xml):
        attributes, body = row_match.group(1), row_match.group(2)
        number = re.search(r'\br="(\d+)"', attributes)
        cells: List[str] = []
        for cell in re.finditer(r"(?is)<c\b([^>]*)(?:/>|>(.*?)</c>)", body):
            cell_attr, cell_body = cell.group(1) or "", cell.group(2) or ""
            kind = re.search(r'\bt="([^"]+)"', cell_attr)
            kind = kind.group(1) if kind else "n"
            style_match = re.search(r'\bs="(\d+)"', cell_attr)
            style_index = int(style_match.group(1)) if style_match else None
            value = ""
            if kind == "inlineStr":
                value = "".join(re.findall(r"(?is)<t[^>]*>(.*?)</t>", cell_body))
            else:
                raw_value = re.search(r"(?is)<v[^>]*>(.*?)</v>", cell_body)
                value = raw_value.group(1) if raw_value else ""
                if kind == "s" and value.strip().isdigit():
                    index = int(value.strip())
                    value = shared[index] if 0 <= index < len(shared) else ""
            value = _normalize(html.unescape(value))
            if kind not in ("s", "inlineStr", "str"):
                # Sel bertipe tanggal: serial Excel diterjemahkan ke tanggal ISO.
                value = sheet_dates.styled_value(value, style_index, styles)
            col = re.search(r'\br="([A-Z]+)\d+"', cell_attr)
            cells.append(f"{col.group(1)}={value}" if col and value else value)
        line = " | ".join(part for part in cells if part)
        if line.strip():
            prefix = f"baris {number.group(1)}: " if number else ""
            rows.append(prefix + line)
    return rows


def _sheet_extras(archive: "zipfile.ZipFile", names: set, member: str) -> List[str]:
    """Header/footer cetak, komentar sel, dan teks dalam objek gambar milik satu lembar."""

    out: List[str] = []
    pieces = member.split("/")
    rels_name = "/".join(pieces[:-1] + ["_rels", pieces[-1] + ".rels"])
    targets: List[str] = []
    if rels_name in names:
        raw = archive.read(rels_name).decode("utf-8", "ignore")
        for tag in re.findall(r"(?is)<Relationship\b[^>]*>", raw):
            target = _tag_value(tag, "target")
            if not target:
                continue
            resolved = target.lstrip("/")
            if not resolved.startswith("xl/"):
                # "worksheets/../comments1.xml" harus jadi "xl/comments1.xml", bukan
                # "xl/worksheets/comments1.xml" - kalau salah, komentar sel hilang.
                resolved = posixpath.normpath("/".join(pieces[:-1] + [resolved]))
            targets.append(resolved)
    for name in targets:
        if name not in names:
            continue
        base = Path(name).name.lower()
        try:
            raw = archive.read(name).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001 - bagian rusak tidak boleh menggagalkan dokumen
            continue
        if base.startswith("comments"):
            joined = " / ".join(_normalize(html.unescape(item))
                                for item in re.findall(r"(?is)<t[^>]*>(.*?)</t>", raw))
            if joined.strip():
                out.append(f"Komentar sel: {joined}")
        elif base.startswith("drawing"):
            joined = " / ".join(_normalize(html.unescape(item))
                                for item in re.findall(r"(?is)<a:t[^>]*>(.*?)</a:t>", raw))
            if joined.strip():
                out.append(f"Teks pada gambar/objek: {joined}")
    return out


def parse_xlsx(content: bytes, document_name: str) -> ParsedDocument:
    """Excel modern (.xlsx/.xlsm): baca sharedStrings + setiap sheet, satu sheet satu halaman."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise AppError("INDEXING_FAILED", f"Unreadable .xlsx: {exc}") from exc
    with archive:
        names = set(archive.namelist())
        styles = sheet_dates.WorkbookStyles()
        if "xl/styles.xml" in names:
            styles = sheet_dates.parse_styles(archive.read("xl/styles.xml").decode("utf-8", "ignore"))
        if "xl/workbook.xml" in names:
            styles.date1904 = sheet_dates.date1904_from_workbook(
                archive.read("xl/workbook.xml").decode("utf-8", "ignore"))
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            raw = archive.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            for item in re.findall(r"(?is)<si\b.*?</si>|<si\b[^>]*/>", raw):
                shared.append(_normalize(html.unescape("".join(re.findall(r"(?is)<t[^>]*>(.*?)</t>", item)))))

        relationships: dict = {}
        if "xl/_rels/workbook.xml.rels" in names:
            raw = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8", "ignore")
            for tag in re.findall(r"(?is)<Relationship\b[^>]*>", raw):
                identifier, target = _tag_value(tag, "id"), _tag_value(tag, "target")
                if identifier and target:
                    relationships[identifier] = target

        sheets: List[tuple] = []
        if "xl/workbook.xml" in names:
            raw = archive.read("xl/workbook.xml").decode("utf-8", "ignore")
            for tag in re.findall(r"(?is)<sheet\b[^>]*>", raw):
                title = _tag_value(tag, "name")
                relation = _tag_value(tag, "r:id") or _tag_value(tag, "id")
                target = relationships.get(relation, "")
                if not relation or not target:
                    continue
                path = target.lstrip("/")
                if not path.startswith("xl/"):
                    path = path.lstrip("./")
                    path = "xl/" + path if not path.startswith("xl/") else path
                sheets.append((title or Path(path).stem, path))
        if not sheets:
            sheets = [(Path(name).stem, name) for name in sorted(names)
                      if name.startswith("xl/worksheets/") and name.endswith(".xml")]

        pages: List[ParsedPage] = []
        for index, (title, path) in enumerate(sheets, start=1):
            if path not in names:
                continue
            sheet_xml = archive.read(path).decode("utf-8", "ignore")
            rows = _xlsx_sheet_rows(sheet_xml, shared, styles)
            lines = [f"Lembar: {title}", *rows]
            # Header/footer cetak sering memuat nama perusahaan, nomor dokumen, atau periode.
            printed = [_normalize(html.unescape(TAG_RE.sub("", found))) for found in re.findall(
                r"(?is)<(?:oddHeader|oddFooter|evenHeader|evenFooter|firstHeader|firstFooter)[^>]*>(.*?)</",
                sheet_xml)]
            printed = [part for part in printed if part]
            if printed:
                lines.append("Header/footer cetak: " + " / ".join(printed))
            lines.extend(_sheet_extras(archive, names, path))
            pages.append(ParsedPage(page=index, text=_normalize("\n".join(lines))))
    if not any(page.text for page in pages):
        raise AppError("INDEXING_FAILED", "Excel workbook has no readable cell text")
    return ParsedDocument(document_name=document_name, pages=pages, parser="xlsx-stdlib")


def parse_pptx(content: bytes, document_name: str) -> ParsedDocument:
    """PowerPoint modern (.pptx): teks per slide + catatan pembicara, satu slide satu halaman."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise AppError("INDEXING_FAILED", f"Unreadable .pptx: {exc}") from exc

    def slide_number(name: str) -> int:
        # case-insensitive: nama berkas catatan adalah "notesSlideN.xml"
        match = re.search(r"(?i)slide(\d+)\.xml$", name)
        return int(match.group(1)) if match else 0

    with archive:
        names = [name for name in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", name)]
        notes = {slide_number(name): name for name in archive.namelist()
                 if re.match(r"ppt/notesSlides/notesSlide\d+\.xml$", name)}
        pages: List[ParsedPage] = []
        for number, name in enumerate(sorted(names, key=slide_number), start=1):
            raw = archive.read(name).decode("utf-8", "ignore")
            raw = re.sub(r"(?is)</a:p>", "\n", raw)
            text = _html_to_text(raw)
            note_name = notes.get(slide_number(name))
            if note_name:
                note_raw = archive.read(note_name).decode("utf-8", "ignore")
                note = _html_to_text(re.sub(r"(?is)</a:p>", "\n", note_raw))
                if note:
                    text = f"{text}\n\nCatatan pembicara: {note}"
            pages.append(ParsedPage(page=number, text=_normalize(text)))
        # Teks di master dan tata letak (nama perusahaan, agenda tetap, footer) tidak
        # muncul di slide mana pun, jadi dibaca terpisah di halaman terakhir.
        extra: List[str] = []
        for pattern, label in (("ppt/slideMasters/slideMaster*.xml", "Master slide"),
                               ("ppt/slideLayouts/slideLayout*.xml", "Tata letak slide")):
            for member in sorted(name for name in archive.namelist() if fnmatch.fnmatch(name, pattern)):
                raw = archive.read(member).decode("utf-8", "ignore")
                text = _html_to_text(re.sub(r"(?is)</a:p>", "\n", raw))
                if text.strip():
                    extra.append(f"[{label} {Path(member).stem}] {_normalize(text)}")
        if extra:
            pages.append(ParsedPage(page=len(pages) + 1, text=_normalize("\n".join(extra))))
    if not any(page.text for page in pages):
        raise AppError("INDEXING_FAILED", "Presentation has no readable text (only images?)")
    return ParsedDocument(document_name=document_name, pages=pages, parser="pptx-stdlib")


def _odf_attribute(attributes: str, name: str) -> str:
    """Atribut ODF dengan atau tanpa awalan namespace (``value-type`` vs ``office:value-type``)."""

    for key, value in _tag_attributes(attributes).items():
        if key == name or key.endswith(":" + name):
            return value
    return ""


def _odf_cell_value(attributes: str, inner: str) -> str:
    """Nilai satu sel ODF: teksnya, atau nilai atribut bila selnya angka/tanggal tanpa teks."""

    text = _normalize(html.unescape(" ".join(re.findall(r"(?is)<text:p[^>]*>(.*?)</text:p>", inner))))
    if text:
        return text
    kind = _odf_attribute(attributes, "value-type").lower()
    if kind in ("date", "time"):
        return _odf_attribute(attributes, "date-value") or _odf_attribute(attributes, "value")
    if kind == "boolean":
        return "TRUE" if _odf_attribute(attributes, "boolean-value").lower() in ("true", "1") else "FALSE"
    return _odf_attribute(attributes, "value")


def parse_odf(content: bytes, document_name: str) -> ParsedDocument:
    """OpenDocument (.odt/.ods/.odp): content.xml + gaya (header/footer) + meta."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            raw = archive.read("content.xml").decode("utf-8", "ignore")
            styles_xml = archive.read("styles.xml").decode("utf-8", "ignore") if "styles.xml" in names else ""
            meta_xml = archive.read("meta.xml").decode("utf-8", "ignore") if "meta.xml" in names else ""
    except Exception as exc:  # noqa: BLE001
        raise AppError("INDEXING_FAILED", f"Unreadable OpenDocument file: {exc}") from exc
    body = raw

    def _flatten_cell(match: "re.Match[str]") -> str:
        inner = re.sub(r"(?is)</text:[hp]>", " ", match.group(2))
        value = _normalize(html.unescape(TAG_RE.sub("", inner)))
        if not value:
            # sel angka/tanggal boleh tidak punya teks sama sekali: nilainya ada di atribut
            value = _odf_cell_value(match.group(1), "")
        return value + " | "

    body = re.sub(r"(?is)<table:table-cell\b([^>]*)/>",
                  lambda match: _normalize(_odf_cell_value(match.group(1), "")) + " | ", body)
    body = re.sub(r"(?is)<table:table-cell\b([^>]*)>(.*?)</table:table-cell>",
                  lambda match: _flatten_cell(match) if match.group(2).strip() else
                  (_normalize(_odf_cell_value(match.group(1), "")) + " | "), body)
    body = re.sub(r"(?is)</text:h>", "\n", body)
    body = re.sub(r"(?is)</text:p>", "\n", body)
    body = re.sub(r"(?is)</table:table-row>", "\n", body)
    body = re.sub(r"(\s*\|\s*)+", " | ", body)  # sel kosong tidak boleh menyisakan ' |  | '
    body = re.sub(r"(?is)<text:tab[^>]*/>", "\t", body)
    body = re.sub(r"(?is)<text:line-break[^>]*/>", "\n", body)
    text = _normalize(html.unescape(TAG_RE.sub("", body)))

    # Header/footer hidup di styles.xml (bukan content.xml) dan sering memuat nama instansi,
    # nomor dokumen, atau periode - jadi ikut dibaca alih-alih dibiarkan hilang.
    extras: List[str] = []
    if styles_xml:
        for pattern, label in ((r"(?is)<style:header\b[^>]*>(.*?)</style:header>", "Header"),
                               (r"(?is)<style:footer\b[^>]*>(.*?)</style:footer>", "Footer")):
            for found in re.findall(pattern, styles_xml):
                part = _normalize(html.unescape(TAG_RE.sub(" ", found)))
                if part:
                    extras.append(f"[{label}] {part}")
    if meta_xml:
        meta = []
        for tag in ("title", "subject", "creator", "description", "keyword", "generator"):
            found = re.search(rf"(?is)<(?:dc:|meta:)?{tag}[^>]*>(.*?)</", meta_xml)
            if found and TAG_RE.sub("", found.group(1)).strip():
                meta.append(f"{tag}: {html.unescape(TAG_RE.sub('', found.group(1))).strip()}")
        if meta:
            extras.append("[Properti dokumen] " + "; ".join(meta))

    pages: List[ParsedPage] = []
    main = "\f".join(part for part in (text, *extras) if part.strip())
    for index, part in enumerate(main.split("\f"), start=1):
        if part.strip():
            pages.append(ParsedPage(page=index, text=_normalize(part)))
    if not pages:
        raise AppError("INDEXING_FAILED", "OpenDocument file has no readable text")
    return ParsedDocument(document_name=document_name, pages=pages, parser="odf-stdlib")


# Kata kontrol RTF yang punya arti bagi teks, sisanya dibuang. Nilai penting: banyak berkas
# RTF dari perkakas lain menempelkan kata kontrol langsung ke teks ("\\parKuota"), padahal
# standar RTF menuntut pembatas. Untuk parser baca-teks, lebih baik teksnya diselamatkan.
_RTF_INLINE = {
    "par": "\n",
    "line": "\n",
    "sect": "\n",
    "row": "\n",
    "cell": " | ",
    "tab": "\t",
    "page": "\f",
    "emdash": "\u2014",
    "endash": "\u2013",
    "emspace": " ",
    "enspace": " ",
    "bullet": "\u2022",
    "lquote": "\u2018",
    "rquote": "\u2019",
    "ldblquote": "\u201c",
    "rdblquote": "\u201d",
}

# Kata kontrol tak berkaitan teks: namanya dipakai untuk memutus "â\'80\'9cparKuota".
_RTF_HARMLESS = (
    "rtf", "ansi", "mac", "pc", "pca", "deff", "deflang", "adeflang", "f", "fs", "cf", "cb",
    "highlight", "b", "i", "ul", "ulnone", "strike", "scaps", "caps", "v", "sub", "super",
    "nosupersub", "qc", "ql", "qr", "qj", "pard", "plain", "brdr", "brdrs", "brdrw", "brsp",
    "trowd", "trgaph", "trqc", "trrh", "cellx", "cl", "clbrdrt", "clbrdrl", "clbrdrb",
    "clbrdrr", "clcbpat", "clcfpat", "clvertalt", "clvertalc", "clmgf", "clmrg", "lang",
    "langfe", "langnp", "pn", "pntext", "pnlvlblt", "pndec", "outl", "li", "ri", "fi", "sa",
    "sb", "sl", "slmult", "widowctrl", "hyphauto", "loch", "hich", "dbch", "af", "aenddoc",
    "red", "green", "blue", "colortbl", "fonttbl", "stylesheet", "info", "generator", "uc",
    "intbl", "nowidctlpar", "adjustright", "keepn", "noline", "sectd", "sbknone", "cols",
    "pardefault", "sbasedon", "snext", "sn", "sp", "cs", "ds", "ts", "expnd", "expndtw",
    "kerning", "dn", "up", "chftn", "chatn", "field", "fldinst", "fldrslt", "shppict",
    "nonshppict", "pict", "objdata", "datastore", "themedata", "wmetafile", "picw", "pich",
    "picwgoal", "pichgoal", "blipuid", "pngblip", "jpegblip", "mmath", "mmathpr", "margl",
    "margr", "margt", "margb", "sectdefaultcl", "ftnbj", "aendnotes", "fet", "lochhps",
)

_RTF_CONTROL_RE = re.compile(r"\\([a-zA-Z]+)(-?\d*) ?")


def _rtf_control_word(match: "re.Match[str]") -> str:
    """Satu kata kontrol. Yang dikenal teks dipetakan, yang tidak dikenal dibuang."""
    word = match.group(1).lower()
    digits = match.group(2) or ""
    if digits:
        return ""  # bernilai: \fs24, \red0, \li720
    if word in _RTF_INLINE:
        return _RTF_INLINE[word]
    if word in _RTF_HARMLESS:
        return ""
    for keyword, replacement in _RTF_INLINE.items():
        if word.startswith(keyword) and word != keyword:
            # "\parKuota" = kata kontrol + teks tanpa pembatas. Spasi yang ikut tertangkap
            # regex milik teks (bukan pembatas), jadi dikembalikan.
            tail = match.group(1)[len(keyword):]
            return replacement + tail + (" " if match.group(0).endswith(" ") else "")
    return ""


RTF_SKIP_DESTINATIONS = ("fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "themedata", "datastore")


def parse_rtf(content: bytes, document_name: str) -> ParsedDocument:
    """Rich Text (.rtf): buang grup kontrol dan baca teksnya (tanpa pustaka tambahan)."""
    raw = content.decode("latin-1", "ignore")
    for destination in RTF_SKIP_DESTINATIONS:
        raw = re.sub(r"\{\*?\\" + destination + r"\b[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", " ", raw, flags=re.S)
    raw = re.sub(r"(?is)\{\\\*[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", " ", raw)
    raw = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "ignore"), raw)
    raw = re.sub(r"\\u(-?\d+)\s?\??", lambda m: chr(int(m.group(1)) % 65536), raw)
    raw = re.sub(r"\\([a-zA-Z]+)(-?\d*) ?", _rtf_control_word, raw)
    raw = raw.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
    raw = re.sub(r"[{}]", "", raw)
    text = _normalize(raw)
    if not text:
        raise AppError("INDEXING_FAILED", "Rich Text file has no readable text")
    return ParsedDocument(document_name=document_name, pages=[ParsedPage(page=1, text=text)], parser="rtf-stdlib")


def parse_epub(content: bytes, document_name: str) -> ParsedDocument:
    """EPUB: ikuti spine OPF supaya urutan bab benar; satu bab satu halaman."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise AppError("INDEXING_FAILED", f"Unreadable .epub: {exc}") from exc
    with archive:
        names = set(archive.namelist())

        def read(name: str) -> str:
            return archive.read(name).decode("utf-8", "ignore")

        opf_path = ""
        if "META-INF/container.xml" in names:
            container = read("META-INF/container.xml")
            match = re.search(r'full-path="([^"]+)"', container)
            if match:
                opf_path = match.group(1)
        chapters: List[str] = []
        if opf_path and opf_path in names:
            base = str(Path(opf_path).parent)
            opf = read(opf_path)
            manifest = {}
            for tag in re.findall(r"(?is)<item\b[^>]*>", opf):
                identifier, href = _tag_value(tag, "id"), _tag_value(tag, "href")
                if identifier and href:
                    manifest[identifier] = (href, _tag_value(tag, "properties"))
            for idref in re.findall(r'(?is)<itemref\b[^>]*idref="([^"]+)"', opf):
                entry = manifest.get(idref)
                if not entry:
                    continue
                href, properties = entry
                if "nav" in properties.split() or Path(href).name.lower() in {"nav.xhtml", "toc.xhtml", "toc.ncx"}:
                    continue  # daftar isi bukan isi buku
                path = str(Path(base) / href) if base and base != "." else href
                path = path.replace("\\", "/").lstrip("./")
                if path in names:
                    chapters.append(path)
        if not chapters:
            chapters = sorted(name for name in names
                              if name.lower().endswith((".xhtml", ".html", ".htm")))
        pages: List[ParsedPage] = []
        for index, path in enumerate(chapters, start=1):
            raw = read(path)
            pages.append(ParsedPage(page=index, text=_html_to_text(raw)))
    if not any(page.text for page in pages):
        raise AppError("INDEXING_FAILED", "EPUB has no readable text")
    return ParsedDocument(document_name=document_name, pages=pages, parser="epub-stdlib")


def parse_csv(content: bytes, document_name: str) -> ParsedDocument:
    """CSV/TSV: pisahkan kolom dengan ' | ' supaya baris tetap terbaca saat dipotong chunk."""
    import csv as csv_module

    text = _decode_text(content)
    sample = text[:4096]
    try:
        dialect = csv_module.Sniffer().sniff(sample, delimiters=",;\t|")
    except Exception:  # noqa: BLE001 - sniffer is a guess, fall back to comma
        dialect = csv_module.excel
    rows = []
    for index, row in enumerate(csv_module.reader(io.StringIO(text), dialect), start=1):
        cells = [cell.strip() for cell in row]
        if any(cells):
            rows.append(f"baris {index}: " + " | ".join(cells))
    body = _normalize("\n".join(rows))
    if not body:
        raise AppError("INDEXING_FAILED", "Delimited file has no rows")
    return ParsedDocument(document_name=document_name, pages=[ParsedPage(page=1, text=body)], parser="csv-stdlib")


def parse_xml(content: bytes, document_name: str) -> ParsedDocument:
    raw = _decode_text(content)
    raw = re.sub(r"(?is)<!--.*?-->", " ", raw)
    raw = re.sub(r"(?is)<\?.*?\?>", " ", raw)
    raw = re.sub(r">\s*<", ">\n<", raw)
    text = _normalize(html.unescape(TAG_RE.sub("", raw)))
    if not text:
        raise AppError("INDEXING_FAILED", "XML has no readable text")
    return ParsedDocument(document_name=document_name, pages=[ParsedPage(page=1, text=text)], parser="xml")


def parse_image_ocr(content: bytes, document_name: str) -> ParsedDocument:
    """Gambar: OCR kalau tesseract tersedia. Kalau tidak, katakan terus terang."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise AppError(
            "UNSUPPORTED_MEDIA_TYPE",
            "OCR belum tersedia di mesin ini (pytesseract/Pillow tidak terpasang)",
            details={"hint": "pasang tesseract-ocr + pytesseract, lalu muat ulang layanan"},
        ) from exc
    try:
        image = Image.open(io.BytesIO(content))
        text = pytesseract.image_to_string(image, lang="ind+eng")
    except Exception as exc:  # noqa: BLE001
        raise AppError("INDEXING_FAILED", f"OCR gagal: {exc}") from exc
    text = _normalize(text)
    if not text:
        raise AppError("INDEXING_FAILED", "OCR tidak menemukan teks pada gambar ini")
    return ParsedDocument(document_name=document_name, pages=[ParsedPage(page=1, text=text)], parser="ocr")


def parse_text(content: bytes, document_name: str) -> ParsedDocument:
    text = _decode_text(content)
    if "\f" in text:  # explicit page breaks
        pages = [_normalize(part) for part in text.split("\f")]
        return ParsedDocument(
            document_name=document_name,
            pages=[ParsedPage(page=i, text=p) for i, p in enumerate(pages, start=1)],
            parser="text",
        )
    numbered = re.split(r"(?im)^\s*\[page\s+(\d+)\]\s*$", text)
    if len(numbered) > 1:
        pages: List[ParsedPage] = []
        for index in range(1, len(numbered), 2):
            pages.append(ParsedPage(page=int(numbered[index]), text=_normalize(numbered[index + 1])))
        return ParsedDocument(document_name=document_name, pages=pages, parser="text")
    return ParsedDocument(document_name=document_name, pages=[ParsedPage(page=1, text=_normalize(text))], parser="text")


def parse_json_document(content: bytes, document_name: str) -> ParsedDocument:
    try:
        payload = json.loads(content.decode("utf-8", "ignore"))
    except Exception as exc:  # noqa: BLE001
        raise AppError("INDEXING_FAILED", f"Invalid JSON document: {exc}") from exc
    if isinstance(payload, dict) and "pages" in payload:
        pages = []
        for index, page in enumerate(payload["pages"], start=1):
            if isinstance(page, dict):
                pages.append(ParsedPage(page=int(page.get("page", index)), text=_normalize(str(page.get("text", "")))))
            else:
                pages.append(ParsedPage(page=index, text=_normalize(str(page))))
        name = str(payload.get("document_name") or document_name)
        return ParsedDocument(document_name=name, pages=pages, parser="json")
    return ParsedDocument(
        document_name=document_name,
        pages=[ParsedPage(page=1, text=_normalize(json.dumps(payload, ensure_ascii=False, indent=2)))],
        parser="json",
    )


def parse_doc_binary(content: bytes, document_name: str) -> ParsedDocument:
    """Word 97-2003 (.doc): teks utama, header/footer, catatan kaki, kotak teks."""

    parts, _meta = doc_binary.read_document(content)
    pages: List[ParsedPage] = []
    for label, text in parts:
        # 0x0C di dokumen Word = pemisah halaman; bagian lain disatukan apa adanya.
        for chunk in text.split("\f"):
            body = chunk.strip()
            if not body:
                continue
            pages.append(ParsedPage(page=len(pages) + 1,
                                    text=body if label == "Teks utama" else f"[{label}]\n{body}"))
    if not pages:
        raise AppError("INDEXING_FAILED", "Dokumen .doc tidak memuat teks yang bisa dibaca")
    return ParsedDocument(document_name=document_name, pages=pages, parser="doc-binary")


def parse_xls_binary(content: bytes, document_name: str) -> ParsedDocument:
    """Excel 97-2003 (.xls): satu lembar satu halaman, nilai sel apa adanya."""

    sheets = xls_binary.read_workbook(content)
    pages: List[ParsedPage] = []
    for index, sheet in enumerate(sheets, start=1):
        rows = sheet.rows()
        if not rows:
            continue
        body = "\n".join([f"Lembar: {sheet.name}"] + [" | ".join(row) for row in rows])
        pages.append(ParsedPage(page=index, text=_normalize(body)))
    if not pages:
        raise AppError("INDEXING_FAILED", "Berkas .xls tidak memuat sel yang bisa dibaca")
    return ParsedDocument(document_name=document_name, pages=pages, parser="xls-binary")


def parse_ppt_binary(content: bytes, document_name: str) -> ParsedDocument:
    """PowerPoint 97-2003 (.ppt): satu slide satu halaman."""

    slides, _meta = ppt_binary.read_presentation(content)
    pages = [ParsedPage(page=index, text=_normalize(f"Slide {index}\n{text}"))
             for index, text in enumerate(slides, start=1) if text.strip()]
    if not pages:
        raise AppError("INDEXING_FAILED", "Presentasi .ppt tidak memuat teks yang bisa dibaca")
    return ParsedDocument(document_name=document_name, pages=pages, parser="ppt-binary")


def parse_eml(content: bytes, document_name: str) -> ParsedDocument:
    """Email .eml: header penting + isi surat (teks/HTML) + daftar lampiran."""

    try:
        message = email.message_from_bytes(content, policy=email.policy.default)
    except Exception as exc:  # noqa: BLE001
        raise AppError("INDEXING_FAILED", f"Email tidak terbaca: {exc}") from exc
    lines: List[str] = []
    for header in ("From", "To", "Cc", "Date", "Subject"):
        value = message.get(header)
        if value:
            lines.append(f"{header}: {value}")
    bodies: List[str] = []
    attachments: List[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        if filename:
            attachments.append(filename)
            continue
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                payload = part.get_content()
            except Exception:  # noqa: BLE001
                payload = ""
            if not isinstance(payload, str):
                continue
            if part.get_content_type() == "text/html":
                payload = _html_to_text(payload)
            if payload.strip():
                bodies.append(_normalize(payload))
    text = "\n\n".join(lines + bodies)
    if attachments:
        text += "\n\nLampiran: " + ", ".join(attachments)
    if not text.strip():
        raise AppError("INDEXING_FAILED", "Email tidak memuat isi yang bisa dibaca")
    return ParsedDocument(document_name=document_name, pages=[ParsedPage(page=1, text=_normalize(text))],
                          parser="eml")


def detect_language(text: str) -> str:
    sample = text[:4000].lower()
    indonesian = sum(sample.count(token) for token in (" dan ", " yang ", " dengan ", " untuk ", " tidak ", " adalah "))
    english = sum(sample.count(token) for token in (" the ", " and ", " with ", " for ", " not ", " is "))
    return "id" if indonesian >= english else "en"


_PARSERS = {
    "pdf": parse_pdf,
    "docx": parse_docx,
    "doc_binary": parse_doc_binary,
    "xls_binary": parse_xls_binary,
    "ppt_binary": parse_ppt_binary,
    "eml": parse_eml,
    "xlsx": parse_xlsx,
    "pptx": parse_pptx,
    "odf": parse_odf,
    "rtf": parse_rtf,
    "epub": parse_epub,
    "csv": parse_csv,
    "xml": parse_xml,
    "html": parse_html,
    "json": parse_json_document,
    "image_ocr": parse_image_ocr,
    "text": parse_text,
}


def _ole_parser(content: bytes) -> str:
    """Tilik stream di dalam kontainer OLE: Word, Excel, atau PowerPoint."""

    try:
        container = doc_binary.ole.OleContainer(content)
        names = set(container.names())
    except Exception:  # noqa: BLE001 - bukan OLE yang sehat
        return ""
    if "WordDocument" in names:
        return "doc_binary"
    if "PowerPoint Document" in names:
        return "ppt_binary"
    if "Workbook" in names or "Book" in names:
        return "xls_binary"
    return ""


def parser_for(document_name: str, content: bytes = b"", declared_mime: str = "") -> str:
    """Pilih kunci parser untuk berkas ini (isi berkas dulu, lalu katalog, lalu MIME)."""
    suffix = Path(document_name).suffix.lower()
    spec = EXTENSION_MAP.get(suffix)
    if content[:4] == b"%PDF":
        return "pdf"
    # Isi berkas lebih dipercaya daripada namanya: berkas .doc yang sebenarnya RTF/HTML
    # (umum dari sistem lama) tetap terbaca, begitu pula .xls yang isinya OOXML.
    head = content[:1024].lstrip().lower()
    if head.startswith(RTF_HEADER) or head.startswith(b"{\
tf"):
        return "rtf"
    if head.startswith(b"<!doctype html") or head.startswith(b"<html") or head.startswith(b"<?xml") and b"<html" in head:
        return "html"
    if content[:8] == OLE_MAGIC:
        detected = _ole_parser(content)
        if detected:
            return detected
    if content[:2] == b"PK" and spec is not None and spec.parser in ("docx", "xlsx", "pptx", "odf"):
        return spec.parser
    if spec is not None:
        return spec.parser
    mime = (declared_mime or "").lower()
    if mime == "application/pdf":
        return "pdf"
    if "wordprocessingml" in mime:
        return "docx"
    if "spreadsheetml" in mime:
        return "xlsx"
    if "presentationml" in mime:
        return "pptx"
    if mime == "application/json":
        return "json"
    if mime.startswith("image/"):
        return "image_ocr"
    if mime.startswith("text/"):
        return "text"
    return ""


def parse_document(content: bytes, document_name: str, declared_mime: str = "") -> ParsedDocument:
    """Route a byte payload to the right parser. Pure function, no I/O."""
    key = parser_for(document_name, content, declared_mime)
    parser = _PARSERS.get(key)
    if parser is None:
        suffix = Path(document_name).suffix.lower()
        raise AppError(
            "UNSUPPORTED_MEDIA_TYPE",
            f"No parser for '{document_name}' ({(declared_mime or suffix) or 'unknown type'})",
        )
    parsed = parser(content, document_name)
    # Bersihkan setiap halaman sebelum lanjut: karakter kontrol dan spasi berlebih dari berkas
    # lama membuat potongan tampak rusak. Dilakukan di sini supaya SEMUA jalur (unggahan,
    # berkas publik, crawl) memakai aturan yang sama.
    for page in parsed.pages:
        page.text = clean_text(page.text)
    parsed.pages = [page for page in parsed.pages if page.text.strip()]
    if not parsed.pages:
        raise AppError("INDEXING_FAILED", f"'{document_name}' tidak memuat teks yang bisa dibaca")

    # Berkas yang isinya sebenarnya biner tetapi lolos jalur teks (mis. .txt berisi dump PDF,
    # atau PDF yang rusak sehingga teksnya keluar sebagai byte) tidak boleh disimpan: potongan
    # seperti itu tidak bisa dicari dan mencemari jawaban model.
    garbage, reason = looks_like_binary_garbage(parsed.text)
    if garbage:
        raise AppError(
            "INDEXING_FAILED",
            f"'{document_name}' tampaknya bukan teks yang bisa dibaca ({reason})",
        )

    parsed.language = detect_language(parsed.text)
    return parsed


def parse_from_path(path: str | Path, declared_mime: str = "") -> ParsedDocument:
    file_path = Path(path)
    return parse_document(file_path.read_bytes(), file_path.name, declared_mime)


def page_for_offset(parsed: ParsedDocument, needle: str, default: Optional[int] = None) -> Optional[int]:
    """Best-effort page attribution for a chunk (PRD 9 transitive metadata)."""
    if not needle:
        return default
    probe = needle.strip()[:80]
    if not probe:
        return default
    for page in parsed.pages:
        if probe in page.text:
            return page.page
    return default
