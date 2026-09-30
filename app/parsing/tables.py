"""Ekstraksi tabel TERSTRUKTUR dari berkas knowledge (xlsx, xlsm, xls, csv, tsv, ods).

Berbeda dari :mod:`app.parsing.parser` yang mengubah isi berkas menjadi teks untuk
diindeks, modul ini mengembalikan baris/kolom apa adanya supaya pertanyaan seperti
"produk apa yang paling laku?" atau "berapa total penjualan?" bisa DIHITUNG dari data
riil -- bukan ditebak oleh LLM.

Aturan penting:

* Sel kosong tetap dipertahankan posisinya (kolom A..G tetap sejajar walau ada sel kosong).
* Lebar tabel diambil dari SELURUH baris, bukan hanya baris pertama: dulu lembar yang dibuka
  dengan baris judul gabungan (mis. "RINGKASAN DATA PENJUALAN" di A1) kehilangan seluruh kolom
  B..F karena lebar tabel dikunci oleh baris judul itu.
* Baris judul tidak dibuang diam-diam: isinya dicatat pada ``TableData.notes``.
* Sel bertipe tanggal ditulis sebagai tanggal ISO, bukan angka serial Excel.
* Tidak ada pembulatan/penafsiran: nilai disimpan sebagai teks apa adanya, konversi angka
  terjadi saat perhitungan (:mod:`app.tables.analytics`).
* Berkas yang bukan tabel (mis. docx/pdf) tidak menghasilkan tabel -- modul ini jujur
  mengembalikan daftar kosong daripada memaksakan struktur.
"""

from __future__ import annotations

import csv
import html
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.errors import AppError
from app.core.logging import get_logger
from app.parsing import sheet_dates, xls_binary

logger = get_logger(__name__)

# Batas aman: dokumen lebih besar dari ini ditolak sebagai tabel (tetap bisa jadi knowledge teks).
MAX_TABLE_ROWS = 200_000
MAX_TABLE_COLUMNS = 64
MAX_REPEATED_COLUMNS = 64

TABLE_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".ods")


@dataclass
class TableData:
    """Satu lembar/berkas tabel yang sudah terstruktur."""

    sheet: str
    headers: List[str]
    rows: List[List[str]]
    source_suffix: str = ""
    truncated: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def schema(self) -> Dict[str, object]:
        """Ringkasan kolom untuk dipakai LLM (hanya nama + contoh nilai)."""
        columns = []
        for index, header in enumerate(self.headers):
            samples = []
            for row in self.rows[:40]:
                if index < len(row) and str(row[index]).strip():
                    samples.append(str(row[index]))
                if len(samples) >= 3:
                    break
            columns.append(
                {
                    "name": header,
                    "index": index,
                    "samples": samples,
                    "numeric": _looks_numeric_column(self.rows, index),
                }
            )
        return {"sheet": self.sheet, "row_count": self.row_count, "columns": columns}

    def as_prompt_rows(self) -> str:
        """Representasi tabel untuk LLM: header diulang + kolom bernama."""
        lines = [" | ".join(self.headers)]
        for row in self.rows:
            cells = [str(row[i]) if i < len(row) else "" for i in range(len(self.headers))]
            lines.append(" | ".join(cells))
        return "\n".join(lines)


# ---------------------------------------------------------------------- #
# Deteksi tabel
# ---------------------------------------------------------------------- #


def supports_tables(document_name: str) -> bool:
    return Path(str(document_name or "")).suffix.lower() in TABLE_SUFFIXES


def extract_tables(content: bytes, document_name: str) -> List[TableData]:
    """Kembalikan daftar tabel; kosong bila berkas bukan tabel yang didukung."""

    suffix = Path(str(document_name or "")).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return _from_xlsx(content, document_name)
    if suffix == ".xls":
        return _from_xls(content, document_name)
    if suffix in (".csv", ".tsv"):
        return _from_delimited(content, document_name, suffix)
    if suffix == ".ods":
        return _from_ods(content, document_name)
    return []


# ---------------------------------------------------------------------- #
# XLSX / XLSM
# ---------------------------------------------------------------------- #


def _from_xlsx(content: bytes, document_name: str) -> List[TableData]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise AppError("INDEXING_FAILED", f"Unreadable .xlsx: {exc}") from exc

    with archive:
        names = set(archive.namelist())
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            raw = archive.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            for item in re.findall(r"(?is)<si\b.*?</si>|<si\b[^>]*/>", raw):
                shared.append(html.unescape("".join(re.findall(r"(?is)<t[^>]*>(.*?)</t>", item))))

        styles = sheet_dates.WorkbookStyles()
        if "xl/styles.xml" in names:
            styles = sheet_dates.parse_styles(archive.read("xl/styles.xml").decode("utf-8", "ignore"))
        if "xl/workbook.xml" in names:
            styles.date1904 = sheet_dates.date1904_from_workbook(
                archive.read("xl/workbook.xml").decode("utf-8", "ignore"))

        relationships: Dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in names:
            raw = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8", "ignore")
            for tag in re.findall(r"(?is)<Relationship\b[^>]*>", raw):
                identifier = _attribute(tag, "id")
                target = _attribute(tag, "target")
                if identifier and target:
                    relationships[identifier] = target

        sheets: List[Tuple[str, str]] = []
        if "xl/workbook.xml" in names:
            raw = archive.read("xl/workbook.xml").decode("utf-8", "ignore")
            for tag in re.findall(r"(?is)<sheet\b[^>]*>", raw):
                title = _attribute(tag, "name")
                target = relationships.get(_attribute(tag, "r:id") or _attribute(tag, "id"), "")
                if target:
                    sheets.append((title or Path(target).stem, target))
        if not sheets:
            sheets = [
                (Path(name).stem, name)
                for name in sorted(names)
                if name.startswith("xl/worksheets/") and name.endswith(".xml")
            ]

        tables: List[TableData] = []
        for title, path in sheets:
            # Target rels bisa absolut ("/xl/worksheets/sheet1.xml") atau relatif
            # ("worksheets/sheet1.xml"); keduanya harus menunjuk anggota arsip yang sama.
            member = path.lstrip("/")
            if not member.startswith("xl/"):
                member = "xl/" + member.lstrip("./")
            if member not in names:
                continue
            rows = _xlsx_rows(archive.read(member).decode("utf-8", "ignore"), shared, styles)
            table = _build_table(title, rows, ".xlsx")
            if table is not None:
                tables.append(table)
        return tables


def _xlsx_rows(xml: str, shared: Sequence[str],
               styles: Optional[sheet_dates.WorkbookStyles] = None) -> List[List[str]]:
    """Baris xlsx sebagai daftar nilai menurut posisi kolom (A, B, C, ...).

    Bila ``styles`` diberikan, sel yang gayanya bertipe tanggal ditulis sebagai tanggal
    ISO; tanpa itu serial mentah (mis. 46075) akan tampak seperti angka biasa.
    """

    out: List[List[str]] = []
    for row_match in re.finditer(r"(?is)<row\b([^>]*)>(.*?)</row>", xml):
        body = row_match.group(2)
        cells: Dict[int, str] = {}
        for cell in re.finditer(r"(?is)<c\b([^>]*)(?:/>|>(.*?)</c>)", body):
            attributes, inner = cell.group(1) or "", cell.group(2) or ""
            reference = re.search(r'\br="([A-Z]+)\d+"', attributes)
            style_match = re.search(r'\bs="(\d+)"', attributes)
            style_index = int(style_match.group(1)) if style_match else None
            kind_match = re.search(r'\bt="([^"]+)"', attributes)
            kind = kind_match.group(1) if kind_match else "n"
            value = ""
            if kind == "inlineStr":
                value = "".join(re.findall(r"(?is)<t[^>]*>(.*?)</t>", inner))
            else:
                raw_value = re.search(r"(?is)<v[^>]*>(.*?)</v>", inner)
                value = raw_value.group(1) if raw_value else ""
                if kind == "s" and value.strip().isdigit():
                    index = int(value.strip())
                    value = shared[index] if 0 <= index < len(shared) else ""
            if reference is None:
                continue
            column = _column_index(reference.group(1))
            if column >= MAX_TABLE_COLUMNS:
                continue
            text = _clean(html.unescape(value))
            if kind not in ("s", "inlineStr", "str"):
                text = sheet_dates.styled_value(text, style_index, styles)
            cells[column] = text
        if cells:
            width = min(MAX_TABLE_COLUMNS, max(cells) + 1)
            out.append([cells.get(index, "") for index in range(width)])
    return out


def _column_index(letters: str) -> int:
    index = 0
    for char in letters.upper():
        index = index * 26 + (ord(char) - 64)
    return index - 1


# ---------------------------------------------------------------------- #
# XLS (biner lama, Word/Excel 97-2003)
# ---------------------------------------------------------------------- #


def _from_xls(content: bytes, document_name: str) -> List[TableData]:
    """Lembar .xls dibaca langsung dari rekaman BIFF (lihat :mod:`app.parsing.xls_binary`)."""

    tables: List[TableData] = []
    for sheet in xls_binary.read_workbook(content):
        table = _build_table(sheet.name, sheet.rows(), ".xls")
        if table is not None:
            tables.append(table)
    return tables


# ---------------------------------------------------------------------- #
# CSV / TSV
# ---------------------------------------------------------------------- #


def _from_delimited(content: bytes, document_name: str, suffix: str) -> List[TableData]:
    text = _decode(content)
    sample = text[:8192]
    if suffix == ".tsv":
        delimiter = "\t"
    else:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except Exception:  # noqa: BLE001
            delimiter = ";"
            header_line = sample.splitlines()[0] if sample.splitlines() else ""
            if header_line.count(",") > header_line.count(";"):
                delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: List[List[str]] = []
    lebih = False
    for raw_row in reader:
        if len(rows) >= MAX_TABLE_ROWS + 1:
            lebih = True
            break
        rows.append([_clean(cell) for cell in raw_row[:MAX_TABLE_COLUMNS]])
    table = _build_table(Path(str(document_name)).stem, rows, suffix, more_rows=lebih)
    return [table] if table is not None else []


def _decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", "ignore")


# ---------------------------------------------------------------------- #
# ODS
# ---------------------------------------------------------------------- #


def _from_ods(content: bytes, document_name: str) -> List[TableData]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise AppError("INDEXING_FAILED", f"Unreadable .ods: {exc}") from exc

    with archive:
        if "content.xml" not in archive.namelist():
            return []
        xml = archive.read("content.xml").decode("utf-8", "ignore")

    tables: List[TableData] = []
    for table_tag in re.finditer(r"(?is)<table:table\b([^>]*)>(.*?)</table:table>", xml):
        name = _attribute(table_tag.group(1), "name") or Path(str(document_name)).stem
        rows: List[List[str]] = []
        lebih = False
        for row_tag in re.finditer(r"(?is)<table:table-row\b[^>]*>(.*?)</table:table-row>", table_tag.group(2)):
            cells: List[str] = []
            for cell in re.finditer(r"(?is)<table:table-cell\b([^>]*)(?:/>|>(.*?)</table:table-cell>)", row_tag.group(1)):
                attributes, inner = cell.group(1) or "", cell.group(2) or ""
                value_type = (_attribute(attributes, "value-type") or "").lower()
                raw_value = _attribute(attributes, "value")
                date_value = _attribute(attributes, "date-value")
                if date_value and value_type in ("date", "time"):
                    # ODS menulis tanggal di office:date-value, bukan sebagai serial
                    value = _clean(date_value.replace("T", " "))[:19]
                elif value_type == "boolean":
                    value = "TRUE" if (raw_value or "").lower() in ("true", "1") else "FALSE"
                elif raw_value is not None:
                    value = _clean(html.unescape(raw_value))
                else:
                    value = _clean(html.unescape(
                        " ".join(re.findall(r"(?is)<text:p[^>]*>(.*?)</text:p>", inner))))
                repeat = _attribute(attributes, "number-columns-repeated")
                times = 1
                if repeat and repeat.isdigit():
                    times = max(1, min(MAX_REPEATED_COLUMNS, int(repeat)))
                cells.extend([value] * times if value else [""] * times)
                if len(cells) > MAX_TABLE_COLUMNS:
                    cells = cells[:MAX_TABLE_COLUMNS]
            if any(cell.strip() for cell in cells):
                rows.append(cells)
            if len(rows) >= MAX_TABLE_ROWS + 1:
                lebih = True
                break
        table = _build_table(name, rows, ".ods", more_rows=lebih)
        if table is not None:
            tables.append(table)
    return tables


# ---------------------------------------------------------------------- #
# Pembentukan tabel (header, pembersihan, batas)
# ---------------------------------------------------------------------- #


def _build_table(sheet: str, rows: List[List[str]], suffix: str,
                 more_rows: bool = False) -> Optional[TableData]:
    """Bentuk satu tabel dari baris mentah.

    ``more_rows`` dipakai pembaca yang memang berhenti di batas baris: tanpa penanda itu,
    berkas berisi tepat di batas akan tampak "utuh" padahal sisa barisnya tidak ikut dibaca.
    """
    cleaned = [row for row in (_strip_row(row) for row in rows) if any(cell for cell in row)]
    if len(cleaned) < 2:
        return None

    notes: List[str] = []

    # Lembar sering dibuka dengan baris judul gabungan yang hanya menempati kolom A
    # (mis. "RINGKASAN DATA PENJUALAN"). Dulu baris itulah yang dianggap header sehingga
    # lebar tabel terkunci di 1 kolom dan SELURUH kolom B..F hilang tanpa jejak.
    start = _header_row_index(cleaned)
    if start > 0:
        title = " / ".join(cell for cell in cleaned[0] if cell)
        if title:
            notes.append(f"judul lembar: {title[:200]}")

    body_source = cleaned[start + 1:]
    width = max(len(cleaned[start]), _widest_row(body_source))
    if width > MAX_TABLE_COLUMNS:
        notes.append(f"kolom dipotong ke {MAX_TABLE_COLUMNS} (aslinya {width})")
        width = MAX_TABLE_COLUMNS
    header_cells = list(cleaned[start]) + [""] * (width - len(cleaned[start]))
    headers = _dedupe_headers(header_cells)

    body: List[List[str]] = []
    for row in body_source:
        padded = list(row) + [""] * (width - len(row))
        body.append(padded[:width])

    # Buang kolom yang benar-benar kosong (mis. sisa kolom formula) -- nama kolomnya dicatat.
    keep = [index for index in range(width) if any(row[index].strip() for row in body)]
    if not keep:
        return None
    dropped = [headers[index] for index in range(width)
               if index not in keep and not headers[index].startswith("kolom_")]
    if dropped:
        notes.append("kolom tanpa isi: " + ", ".join(dropped[:10]))
    headers = [headers[index] for index in keep]
    body = [[row[index] for index in keep] for row in body]

    truncated = False
    if len(body) > MAX_TABLE_ROWS:
        notes.append(f"hanya {MAX_TABLE_ROWS} baris pertama dari {len(body)} baris yang dipakai")
        body = body[:MAX_TABLE_ROWS]
        truncated = True
    elif more_rows:
        # Pembaca berhenti di batas baris: sisanya ada di berkas tetapi tidak dibaca.
        notes.append(f"berkas memuat lebih dari {MAX_TABLE_ROWS} baris; hanya {len(body)} baris "
                     f"pertama yang disimpan")
        truncated = True
    if not body:
        return None
    return TableData(sheet=sheet or "tabel", headers=headers, rows=body, source_suffix=suffix,
                     truncated=truncated, notes=notes)


def _header_row_index(rows: Sequence[Sequence[str]], lookahead: int = 8) -> int:
    """Baris pertama yang punya minimal dua sel terisi; 0 bila tidak ada (tabel satu kolom)."""

    limit = min(len(rows) - 1, lookahead)
    for index in range(limit + 1):
        if sum(1 for cell in rows[index] if cell.strip()) >= 2:
            return index
    return 0


def _widest_row(rows: Sequence[Sequence[str]], cap: int = MAX_TABLE_COLUMNS, sample: int = 500) -> int:
    """Lebar terisi terbesar dari seluruh baris (posisi kolom setelah baris kosong dibuang)."""

    widest = 0
    for row in rows[:sample]:
        if len(row) > widest:
            widest = len(row)
            if widest >= cap:
                return cap
    return widest


def scope_note(table: "TableData") -> str:
    """Kalimat jujur tentang cakupan tabel: dipotong atau ada bagian yang dilewati.

    Dipakai jalur jawaban supaya pemotongan baris/kolom tidak pernah tersembunyi dari pengguna.
    """

    if table is None:
        return ""
    parts: List[str] = []
    if table.truncated:
        parts.append(f"tabel ini dipotong: hanya {table.row_count} baris tersimpan, "
                     f"jadi angkanya dihitung dari baris itu saja")
    for note in list(getattr(table, "notes", []) or [])[:2]:
        parts.append(str(note))
    if not parts:
        return ""
    return "Catatan tabel: " + "; ".join(parts) + "."


def _strip_row(row: Sequence[str]) -> List[str]:
    values = [_clean(cell) for cell in row]
    while values and not values[-1]:
        values.pop()
    return values


def _dedupe_headers(row: Sequence[str]) -> List[str]:
    headers: List[str] = []
    seen: Dict[str, int] = {}
    for index, raw in enumerate(row):
        name = _clean(raw) or f"kolom_{index + 1}"
        name = re.sub(r"\s+", " ", name)
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        seen.setdefault(name, 1)
        headers.append(name[:80])
    return headers


def _clean(value: Optional[str]) -> str:
    if value is None:
        return ""
    return re.sub(r"[ \t\u00a0]+", " ", str(value)).strip()


def _attribute(tag: str, name: str) -> Optional[str]:
    """Atribut XML tanpa peduli urutan MAUPUN huruf besar/kecil.

    Excel menulis ``<Relationship Target=... Id=...>`` (huruf besar) sementara ODS
    memakai huruf kecil; pencocokan case-sensitive pernah membuat nama lembar jatuh
    ke "sheet1".
    """

    match = re.search(rf'\b{re.escape(name)}\s*=\s*"([^"]*)"', tag or "", re.IGNORECASE)
    return match.group(1) if match else None


def _looks_numeric_column(rows: Sequence[Sequence[str]], index: int) -> bool:
    filled = 0
    numeric = 0
    for row in rows[:200]:
        if index >= len(row):
            continue
        value = str(row[index]).strip()
        if not value:
            continue
        filled += 1
        if _is_number_like(value):
            numeric += 1
    return filled > 0 and numeric / filled >= 0.6


def _is_number_like(value: str) -> bool:
    candidate = re.sub(r"[^0-9,.\-]", "", value)
    if not candidate or not any(char.isdigit() for char in candidate):
        return False
    return bool(re.fullmatch(r"-?[\d.,]+", candidate))
