"""Pembaca Excel lama (.xls, BIFF8) memakai pustaka standar.

Berkas .xls adalah kontainer OLE (dibaca ``app/parsing/ole.py``) berisi stream
``Workbook`` (atau ``Book`` untuk versi sangat lama) yang isinya rekaman biner BIFF.

Yang dijaga di sini bukan hanya "bisa dibaca", tetapi **tidak ada isi yang hilang**:

* semua lembar dibaca (``BOUNDSHEET``), termasuk lembar tersembunyi;
* teks dari ``SST`` + ``CONTINUE`` (string panjang yang dipotong antar rekaman),
  ``LABELSST``, ``LABEL``, ``RK``/``MULRK``, ``NUMBER``, ``BOOLERR``, ``FORMULA``+``STRING``;
* sel bertipe **tanggal/waktu** dikenali dari format sel (``XF`` + ``FORMAT``) lalu
  ditulis sebagai tanggal ISO - bukan angka serial seperti 46075;
* sel kosong tetap mempertahankan posisi kolomnya (baris 1, kolom kosong, dst).

Tidak ada ``xlrd``/``olefile``/``pandas`` di jalur ini.
"""

from __future__ import annotations

import datetime as dt
import struct
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.errors import AppError
from app.parsing import ole

# --- id rekaman BIFF ------------------------------------------------------- #
_R_BOF = 0x0809
_R_EOF = 0x000A
_R_BOUNDSHEET = 0x0085
_R_SST = 0x00FC
_R_CONTINUE = 0x003C
_R_LABELSST = 0x00FD
_R_LABEL = 0x0204
_R_RK = 0x027E
_R_MULRK = 0x00BD
_R_NUMBER = 0x0203
_R_FORMULA = 0x0006
_R_STRING = 0x0207
_R_BOOLERR = 0x0205
_R_BLANK = 0x0201
_R_MULBLANK = 0x00BE
_R_XF = 0x00E0
_R_FORMAT = 0x041E
_R_DATEMODE = 0x0022
_R_CODEPAGE = 0x0042

_BOF_WORKSHEET = 0x0010

# Format bawaan Excel yang berarti tanggal/waktu (ECMA-376 bagian 18.8.30).
_BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))
_DATE_TOKENS = ("yy", "dd", "mm:", "mm-", "mm/", "mmm", "d/m", "m/d", "h:m", "am/pm", "[h]")


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _f64(data: bytes, offset: int) -> float:
    return struct.unpack_from("<d", data, offset)[0]


class _UnicodeReader:
    """Pembaca string BIFF8 yang bisa menyeberang ke rekaman CONTINUE."""

    def __init__(self, segments: List[bytes]) -> None:
        self._segments = segments
        self._segment = 0
        self._offset = 0

    def _ensure(self, size: int) -> bool:
        while self._segment < len(self._segments):
            if self._offset + size <= len(self._segments[self._segment]):
                return True
            self._segment += 1
            self._offset = 0
        return False

    def take(self, size: int) -> bytes:
        out = bytearray()
        remaining = size
        while remaining > 0:
            if not self._ensure(1):
                break
            chunk = self._segments[self._segment]
            available = min(remaining, len(chunk) - self._offset)
            out += chunk[self._offset:self._offset + available]
            self._offset += available
            remaining -= available
        return bytes(out)

    def u16(self) -> int:
        raw = self.take(2)
        return _u16(raw + b"\x00\x00", 0)

    def u32(self) -> int:
        raw = self.take(4)
        return _u32(raw + b"\x00" * 4, 0)

    def byte(self) -> int:
        raw = self.take(1)
        return raw[0] if raw else 0

    def string16(self) -> str:
        """XLUnicodeString: panjang 16-bit, bendera 8-bit, lalu karakter."""
        length = self.u16()
        return self._string_body(length)

    def string8(self) -> str:
        length = self.byte()
        return self._string_body(length)

    def _string_body(self, length: int) -> str:
        flags = self.byte()
        wide = bool(flags & 0x01)
        rich = bool(flags & 0x08)
        runs = self.u16() if rich else 0
        ext = self.u32() if (flags & 0x04) else 0
        raw = self.take(length * (2 if wide else 1))
        text = raw.decode("utf-16-le", "replace") if wide else raw.decode("cp1252", "replace")
        if runs:
            self.take(runs * 4)
        if ext:
            self.take(ext)
        return text

    def at_end(self) -> bool:
        return self._segment >= len(self._segments) - 1 and self._offset >= len(
            self._segments[-1] if self._segments else b""
        )


def _records(stream: bytes) -> List[Tuple[int, bytes]]:
    out: List[Tuple[int, bytes]] = []
    offset = 0
    size = len(stream)
    while offset + 4 <= size:
        record_id, length = struct.unpack_from("<HH", stream, offset)
        offset += 4
        body = stream[offset:offset + length]
        out.append((record_id, body))
        offset += length
        if record_id == _R_EOF:
            break
    return out


def _decode_rk(value: int) -> float:
    if value & 0x02:
        number = float(value >> 2)
    else:
        number = _f64(struct.pack("<Q", (value & 0xFFFFFFFC) << 32), 0)
    if value & 0x01:
        number /= 100.0
    return number


def _is_date_format(code: str) -> bool:
    lowered = (code or "").lower()
    if not lowered:
        return False
    cleaned = lowered.replace("\\", "")
    return any(token in cleaned for token in _DATE_TOKENS)


def _format_number(value: float) -> str:
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def _serial_to_text(serial: float, date1904: bool) -> str:
    base = dt.datetime(1904, 1, 1) if date1904 else dt.datetime(1899, 12, 30)
    return (base + dt.timedelta(days=float(serial))).strftime("%Y-%m-%d")


class SheetData:
    def __init__(self, name: str) -> None:
        self.name = name
        self.cells: Dict[Tuple[int, int], str] = {}

    def rows(self) -> List[List[str]]:
        if not self.cells:
            return []
        max_row = max(row for row, _ in self.cells)
        max_column = max(column for _, column in self.cells)
        return [
            [self.cells.get((row, column), "") for column in range(max_column + 1)]
            for row in range(max_row + 1)
        ]

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "rows": self.rows()}


def read_workbook(content: bytes) -> List[SheetData]:
    """Baca seluruh lembar dari berkas .xls. Melempar AppError bila bukan BIFF yang dikenal."""

    container = ole.OleContainer(content)
    stream_name = "Workbook" if container.has("Workbook") else ("Book" if container.has("Book") else "")
    if not stream_name:
        raise AppError("INDEXING_FAILED", "stream Workbook/Book tidak ada di berkas .xls")
    records = _records(container.read(stream_name))

    # ---------------------------------------------------------------- global
    sheets: List[Tuple[str, int]] = []
    xf_formats: List[int] = []
    formats: Dict[int, str] = {}
    shared: List[str] = []
    date1904 = False
    utf16_codepage = False

    index = 0
    while index < len(records):
        record_id, body = records[index]
        if record_id == _R_BOF and len(body) >= 4 and _u16(body, 2) == _BOF_WORKSHEET:
            break  # substream lembar pertama: bagian global sudah selesai
        if record_id == _R_BOUNDSHEET and len(body) >= 8:
            offset = _u32(body, 0)
            name = _UnicodeReader([body[6:]]).string8()
            sheets.append((name, offset))
        elif record_id == _R_XF and len(body) >= 4:
            xf_formats.append(_u16(body, 2))
        elif record_id == _R_FORMAT and len(body) >= 4:
            reader = _UnicodeReader([body[2:]])
            formats[_u16(body, 0)] = reader.string16()
        elif record_id == _R_SST:
            segments = [body]
            look = index + 1
            while look < len(records) and records[look][0] == _R_CONTINUE:
                segments.append(records[look][1])
                look += 1
            reader = _UnicodeReader(segments)
            total = reader.u32()
            unique = reader.u32()
            for _ in range(min(unique, total)):
                shared.append(reader.string16())
            index = look - 1
        elif record_id == _R_DATEMODE and len(body) >= 2:
            date1904 = _u16(body, 0) == 1
        elif record_id == _R_CODEPAGE and len(body) >= 2:
            utf16_codepage = _u16(body, 0) == 1200
        index += 1

    # ---------------------------------------------------------------- lembar
    sheet_by_offset: Dict[int, SheetData] = {}
    for position, (name, offset) in enumerate(sheets):
        sheet_by_offset[position] = SheetData(name or f"Sheet{position + 1}")

    # Zona substream per lembar (dari BOUNDSHEET offset ke BOF berikutnya).
    starts = sorted((offset, position) for position, (_name, offset) in enumerate(sheets))
    boundaries: List[Tuple[int, int]] = []
    for order, (offset, position) in enumerate(starts):
        end = starts[order + 1][0] if order + 1 < len(starts) else len(container.read(stream_name))
        boundaries.append((offset, end))
    position_by_offset = {offset: position for offset, position in starts}

    def cell_text(xf: int, raw: str, numeric: Optional[float]) -> str:
        if numeric is None:
            return raw
        format_index = xf_formats[xf] if 0 <= xf < len(xf_formats) else 0
        code = formats.get(format_index, "")
        is_date = format_index in _BUILTIN_DATE_FORMATS or _is_date_format(code)
        if is_date and numeric > 0:
            return _serial_to_text(numeric, date1904)
        return _format_number(numeric)

    for offset, end in boundaries:
        payload = container.read(stream_name)[offset:end]
        sheet = sheet_by_offset[position_by_offset[offset]]
        pending_formula: Optional[Tuple[int, int, int]] = None
        for record_id, body in _records(payload):
            if record_id in (_R_BOF, _R_EOF):
                continue
            if record_id == _R_LABELSST and len(body) >= 10:
                row, column, xf, item = _u16(body, 0), _u16(body, 2), _u16(body, 4), _u32(body, 6)
                sheet.cells[(row, column)] = shared[item] if 0 <= item < len(shared) else ""
            elif record_id == _R_LABEL and len(body) >= 8:
                row, column = _u16(body, 0), _u16(body, 2)
                sheet.cells[(row, column)] = _UnicodeReader([body[6:]]).string16()
            elif record_id == _R_RK and len(body) >= 10:
                row, column, xf = _u16(body, 0), _u16(body, 2), _u16(body, 4)
                value = _decode_rk(_u32(body, 6))
                sheet.cells[(row, column)] = cell_text(xf, _format_number(value), value)
            elif record_id == _R_MULRK and len(body) >= 6:
                row = _u16(body, 0)
                first = _u16(body, 2)
                count = (len(body) - 6) // 6
                for step in range(count):
                    xf = _u16(body, 4 + step * 6)
                    value = _decode_rk(_u32(body, 6 + step * 6))
                    sheet.cells[(row, first + step)] = cell_text(xf, _format_number(value), value)
            elif record_id == _R_NUMBER and len(body) >= 14:
                row, column, xf = _u16(body, 0), _u16(body, 2), _u16(body, 4)
                value = _f64(body, 6)
                sheet.cells[(row, column)] = cell_text(xf, _format_number(value), value)
            elif record_id == _R_FORMULA and len(body) >= 14:
                row, column, xf = _u16(body, 0), _u16(body, 2), _u16(body, 4)
                result = _f64(body, 6)
                if body[12] == 0xFF and body[13] == 0xFF:
                    kind = body[6] if len(body) > 6 else 0
                    if kind == 0x01:  # boolean
                        sheet.cells[(row, column)] = "TRUE" if body[8] else "FALSE"
                    elif kind == 0x02:  # error
                        sheet.cells[(row, column)] = cell_text(xf, "", None)
                    elif kind == 0x00:  # hasil string ada di rekaman STRING berikutnya
                        pending_formula = (row, column, xf)
                else:
                    sheet.cells[(row, column)] = cell_text(xf, _format_number(result), result)
            elif record_id == _R_STRING and pending_formula is not None:
                row, column, _xf = pending_formula
                sheet.cells[(row, column)] = _UnicodeReader([body]).string16()
                pending_formula = None
            elif record_id == _R_BOOLERR and len(body) >= 8:
                row, column = _u16(body, 0), _u16(body, 2)
                sheet.cells[(row, column)] = "TRUE" if body[6] else "FALSE" if body[7] == 0 else "#ERR"
            elif record_id == _R_BLANK and len(body) >= 6:
                sheet.cells.setdefault((_u16(body, 0), _u16(body, 2)), "")
            elif record_id == _R_MULBLANK and len(body) >= 6:
                row, first = _u16(body, 0), _u16(body, 2)
                count = max(0, (len(body) - 6) // 2)
                for step in range(count):
                    sheet.cells.setdefault((row, first + step), "")

    sheets_out: List[SheetData] = []
    for position in range(len(sheets)):
        sheet = sheet_by_offset.get(position)
        if sheet is not None and sheet.cells:
            sheets_out.append(sheet)
    return sheets_out


def rows_for_sheet(content: bytes, sheet_name: Optional[str] = None) -> List[List[str]]:
    for sheet in read_workbook(content):
        if sheet_name is None or sheet.name == sheet_name:
            return sheet.rows()
    return []


def text_of(rows: Sequence[Sequence[str]]) -> str:
    return "\n".join(" | ".join(cell for cell in row if cell) for row in rows)
