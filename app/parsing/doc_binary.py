"""Pembaca Word lama (.doc, format biner Word 97/2000) memakai pustaka standar.

Struktur yang dibaca:

* ``WordDocument`` - FIB (File Information Block) di awal stream;
* ``0Table``/``1Table`` - tabel teks, tempat **piece table** (CLX) berada untuk dokumen
  yang teksnya tidak berurutan (potongan ANSI dan potongan Unicode dalam satu berkas);
* bila dokumen sederhana (``fComplex`` = 0), teks diambil langsung dari ``fcMin``..``fcMac``.

Isi yang diambil bukan hanya teks utama: bagian catatan kaki, header/footer, anotasi,
endnote, dan textbox ikut dibaca (masing-masing diberi label), supaya tidak ada bagian
dokumen yang hilang begitu saja. Kode kontrol Word diterjemahkan ke bentuk yang bisa dibaca
(paragraf, tab, pemisah halaman) dan kode instruksi field dibuang, bukan dianggap teks.

Tidak ada ``textract``/``antiword``/LibreOffice di jalur ini.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.errors import AppError
from app.parsing import ole

_WORD_MAGIC = (0xA5EC, 0xA5DC)

# Bagian dokumen (ccp*): label yang ditulis ke teks supaya asal potongan tetap jelas.
_SECTIONS: Tuple[Tuple[int, str], ...] = (
    (0x004C, "Teks utama"),
    (0x0050, "Catatan kaki"),
    (0x0054, "Header/Footer"),
    (0x0058, "Makro"),
    (0x005C, "Komentar"),
    (0x0060, "Catatan akhir"),
    (0x0064, "Kotak teks"),
    (0x0068, "Kotak teks header"),
)

_FIELD_BEGIN = 0x13
_FIELD_SEPARATOR = 0x14
_FIELD_END = 0x15


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _clean_control_chars(raw: str) -> str:
    """Terjemahkan kode kontrol Word tanpa membuang teksnya."""

    out: List[str] = []
    in_instruction = False
    for char in raw:
        code = ord(char)
        if code == _FIELD_BEGIN:
            in_instruction = True
            continue
        if code == _FIELD_SEPARATOR:
            in_instruction = False
            continue
        if code == _FIELD_END:
            in_instruction = False
            continue
        if in_instruction:
            continue
        if code == 0x0D:      # akhir paragraf
            out.append("\n")
        elif code == 0x07:    # pemisah sel tabel
            out.append("\t")
        elif code == 0x0C:                 # pemisah halaman: dipertahankan sebagai batas halaman
            out.append("\f")
        elif code in (0x0B, 0x0E):         # ganti baris / ganti kolom
            out.append("\n")
        elif code == 0x09:
            out.append("\t")
        elif code in (0x1E, 0x1F):         # tanda hubung khusus
            out.append("-" if code == 0x1E else "")
        elif code < 0x20 or code == 0x7F:  # penanda objek/gambar/catatan
            continue
        else:
            out.append(char)
    text = "".join(out)
    return text.replace("\x00", "")


def _extract_pieces(document: bytes, table: bytes, fc_clx: int, lcb_clx: int,
                    ccp_total: int, sections: Dict[str, int]) -> List[Tuple[str, str]]:
    """Baca piece table (CLX) lalu kembalikan potongan teks per bagian dokumen."""

    blob = table[fc_clx:fc_clx + lcb_clx]
    if len(blob) < 5:
        return []
    index = 0
    while index < len(blob) and blob[index] == 0x01:      # Prc: lewati (data grpprl)
        if index + 3 > len(blob):
            return []
        size = _u16(blob, index + 1)
        index += 3 + size
    if index >= len(blob) or blob[index] != 0x02:
        return []
    lcb = _u32(blob, index + 1)
    table_data = blob[index + 5:index + 5 + lcb]
    if len(table_data) < 4:
        return []
    pieces = (len(table_data) - 4) // 12
    if pieces <= 0:
        return []

    cps = [_u32(table_data, offset * 4) for offset in range(pieces + 1)]
    pieces_out: List[Tuple[int, int, str]] = []     # (cp_start, cp_end, teks)
    for number in range(pieces):
        base = (pieces + 1) * 4 + number * 8
        fc_raw = _u32(table_data, base + 2)
        compressed = bool(fc_raw & 0x40000000)     # bit 30: satu byte per karakter
        offset = fc_raw & 0x3FFFFFFF
        char_count = cps[number + 1] - cps[number]
        if compressed:
            text = document[offset // 2: offset // 2 + char_count].decode("cp1252", "replace")
        else:
            text = document[offset: offset + char_count * 2].decode("utf-16-le", "replace")
        pieces_out.append((cps[number], cps[number + 1], text))

    def slice_range(start: int, end: int) -> str:
        chunks: List[str] = []
        for cp_start, cp_end, text in pieces_out:
            if cp_end <= start or cp_start >= end:
                continue
            offset_start = max(0, start - cp_start)
            offset_end = min(cp_end - cp_start, end - cp_start)
            chunks.append(text[offset_start:offset_end])
        return "".join(chunks)

    out: List[Tuple[str, str]] = []
    for _offset, label in _SECTIONS:
        start = int(sections.get(label, 0))
        length = int(sections.get(f"{label}__len", 0))
        if length <= 0:
            continue
        text = _clean_control_chars(slice_range(start, start + length))
        if text.strip():
            out.append((label, text))
    return out


def read_document(content: bytes) -> Tuple[List[Tuple[str, str]], Dict[str, object]]:
    """Kembalikan (bagian teks, metadata). Bagian = (label, teks)."""

    container = ole.OleContainer(content)
    if not container.has("WordDocument"):
        raise AppError("INDEXING_FAILED", "stream WordDocument tidak ada di berkas .doc")
    document = container.read("WordDocument")
    if len(document) < 128:
        raise AppError("INDEXING_FAILED", "stream WordDocument terlalu pendek")

    magic = _u16(document, 0)
    if magic not in _WORD_MAGIC:
        raise AppError("INDEXING_FAILED", f"FIB Word tidak dikenal (0x{magic:04X})")

    flags = _u16(document, 0x000A)
    complex_table = bool(flags & 0x0004)
    table_name = "1Table" if (flags & 0x0200) else "0Table"
    table = container.read(table_name) if container.has(table_name) else b""
    fc_min = _u32(document, 0x0018)
    fc_mac = _u32(document, 0x001C)
    ccp_text = _u32(document, 0x004C)

    sections: Dict[str, int] = {}
    cursor = 0
    for offset, name in _SECTIONS:
        length = _u32(document, offset)
        sections[name] = cursor
        sections[f"{name}__len"] = length
        cursor += length

    meta: Dict[str, object] = {
        "nFib": _u16(document, 0x0002),
        "kompleks": complex_table,
        "tabel": table_name,
        "ccpText": ccp_text,
    }

    parts: List[Tuple[str, str]] = []
    if complex_table and table:
        fc_clx = _u32(document, 0x01A2)
        lcb_clx = _u32(document, 0x01A6)
        try:
            parts = _extract_pieces(document, table, fc_clx, lcb_clx, ccp_text, sections)
        except Exception:  # noqa: BLE001 - piece table rusak: jatuh ke pembacaan sederhana
            parts = []
        meta["piece_table"] = True

    if not parts:
        # Dokumen sederhana atau piece table tidak terbaca: teks berurutan fcMin..fcMac.
        raw = document[fc_min:fc_mac]
        if raw:
            text = raw.decode("cp1252", "replace") if not raw[1:2] == b"\x00" else raw.decode(
                "utf-16-le", "replace")
            parts = [("Teks utama", _clean_control_chars(text))]
        meta["piece_table"] = False

    meta["bagian"] = [label for label, text in parts if text.strip()]
    return parts, meta


def text_of_parts(parts: Sequence[Tuple[str, str]]) -> str:
    """Gabungkan bagian menjadi teks yang enak dibaca; label hanya bila bagiannya bukan teks utama."""

    blocks: List[str] = []
    for label, text in parts:
        body = text.strip()
        if not body:
            continue
        blocks.append(body if label == "Teks utama" else f"[{label}]\n{body}")
    return "\n\n".join(blocks)
