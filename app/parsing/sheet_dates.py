"""Penerjemah sel bertipe tanggal pada spreadsheet modern (xlsx/xlsm) dan gaya selnya.

Sel tanggal di Excel disimpan sebagai **angka serial** (mis. ``46075``) dengan gaya sel
bertipe tanggal. Kalau gaya itu diabaikan, teks knowledge berisi "46075" dan kolom tanggal
tampak seperti kolom angka - artinya data pengguna berubah arti tanpa peringatan.
Modul ini membaca ``xl/styles.xml`` (dan ``date1904`` dari ``xl/workbook.xml``) supaya
serial itu bisa ditulis kembali sebagai tanggal ISO.

Dipakai bersama oleh ``app/parsing/parser.py`` (teks knowledge) dan ``app/parsing/tables.py``
(baris tabel untuk perhitungan) - satu tempat, satu perilaku.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional, Tuple

BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))
_DATE_TOKENS = ("yy", "dd", "mm:", "mm-", "mm/", "mmm", "d/m", "m/d", "d-m", "h:m", "am/pm", "[h]", "ss")

_EPOCH = dt.datetime(1899, 12, 30)
_EPOCH_1904 = dt.datetime(1904, 1, 1)


def is_date_format(code: str) -> bool:
    cleaned = (code or "").lower().replace("\\", "")
    if not cleaned:
        return False
    # "m" sendirian ambigu (menit vs bulan); token lain cukup menentukan
    return any(token in cleaned for token in _DATE_TOKENS)


def serial_to_text(serial: float, date1904: bool = False) -> str:
    base = _EPOCH_1904 if date1904 else _EPOCH
    try:
        moment = base + dt.timedelta(days=float(serial))
    except (OverflowError, ValueError):
        return str(serial)
    if moment.hour or moment.minute or moment.second:
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    return moment.strftime("%Y-%m-%d")


class WorkbookStyles:
    """Peta gaya sel -> apakah sel itu tanggal (dan format kode aslinya)."""

    def __init__(self, date_style_ids: Optional[set] = None, formats: Optional[Dict[int, str]] = None,
                 date1904: bool = False) -> None:
        self.date_style_ids = date_style_ids or set()
        self.formats = formats or {}
        self.date1904 = date1904

    @property
    def empty(self) -> bool:
        return not self.date_style_ids

    def is_date_style(self, style_index: Optional[int]) -> bool:
        return style_index is not None and style_index in self.date_style_ids


def parse_styles(styles_xml: str, *, date1904: bool = False) -> WorkbookStyles:
    """Baca ``styles.xml``: format kustom + daftar cellXf yang bertipe tanggal."""

    formats: Dict[int, str] = {}
    for match in re.finditer(r"(?is)<numFmt\b([^>]*)/?>", styles_xml or ""):
        attributes = match.group(1)
        identifier = re.search(r'numFmtId="(\d+)"', attributes)
        code = re.search(r'formatCode="([^"]*)"', attributes)
        if identifier and code:
            formats[int(identifier.group(1))] = code.group(1)

    date_ids: set = set()
    cell_xfs = re.search(r"(?is)<cellXfs\b[^>]*>(.*?)</cellXfs>", styles_xml or "")
    if cell_xfs:
        for index, xf in enumerate(re.finditer(r"(?is)<xf\b([^>]*?)/?>", cell_xfs.group(1))):
            identifier = re.search(r'numFmtId="(\d+)"', xf.group(1))
            if not identifier:
                continue
            format_id = int(identifier.group(1))
            code = formats.get(format_id, "")
            if format_id in BUILTIN_DATE_FORMATS or is_date_format(code):
                date_ids.add(index)
    return WorkbookStyles(date_ids, formats, date1904)


def date1904_from_workbook(workbook_xml: str) -> bool:
    match = re.search(r"(?is)<workbookPr\b[^>]*>", workbook_xml or "")
    if not match:
        return False
    return bool(re.search(r'date1904="(1|true)"', match.group(0), re.IGNORECASE))


def styled_value(raw: str, style_index: Optional[int], styles: Optional[WorkbookStyles]) -> str:
    """Nilai teks satu sel: tanggal diterjemahkan, angka lain dibiarkan apa adanya."""

    if not raw:
        return ""
    if styles is None or styles.empty or style_index is None or not styles.is_date_style(style_index):
        return raw
    try:
        serial = float(raw)
    except (TypeError, ValueError):
        return raw
    result = serial_to_text(serial, styles.date1904)
    # serial yang tidak masuk akal (mis. 0 atau > tahun 9999) sebaiknya tidak dikarang
    if result.startswith("9999") or result.startswith("0001") or result.startswith("1899") or result.startswith("1900-01"):
        return raw
    return result


def looks_like_date_column(header: str, values: List[str]) -> bool:
    """Dugaan kolom tanggal dari nama kolom + isinya, untuk kolom yang tidak bergaya tanggal."""

    name = (header or "").strip().lower()
    named = any(token in name for token in ("tanggal", "tgl", "date", "waktu", "time", "periode"))
    if not named:
        return False
    samples = [value for value in values[:50] if value]
    if not samples:
        return False
    iso = sum(1 for value in samples if re.fullmatch(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?", value))
    return iso / len(samples) >= 0.6
