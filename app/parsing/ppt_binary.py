"""Pembaca PowerPoint lama (.ppt, format biner PowerPoint 97/2000) memakai pustaka standar.

Stream ``PowerPoint Document`` berisi deretan **record** berstruktur:

* record ber-``recVer`` 0xF adalah **container** (Document, Slide, MainMaster, Notes) dan
  isinya dibaca bertingkat;
* teks ada di atom ``TextCharsAtom`` (UTF-16), ``TextBytesAtom`` (satu byte/karakter),
  dan ``CString``;
* satu container ``Slide`` = satu halaman presentasi; urutannya dipertahankan, jadi
  "slide 1", "slide 2", dst. tetap sinkron dengan aslinya.

Yang penting di sini: setiap slide dibaca sampai habis (semua atom teks di dalamnya),
termasuk teks di dalam grup bentuk, dan teks master/catatan bila berada di Document yang
sama - supaya tidak ada kalimat yang tertinggal.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from app.core.errors import AppError
from app.parsing import ole

RT_DOCUMENT = 1000
RT_SLIDE = 1006
RT_NOTES = 1008
RT_MAIN_MASTER = 1016
RT_TEXT_CHARS = 4000
RT_TEXT_BYTES = 4008
RT_CSTRING = 4026
RT_SLIDE_PERSIST = 1011

_CONTAINER_KINDS = {RT_DOCUMENT, RT_SLIDE, RT_NOTES, RT_MAIN_MASTER, 1007, 1017, 1012, 1013, 1014, 1015}
_MAX_DEPTH = 24


class _Slide:
    def __init__(self, kind: int) -> None:
        self.kind = kind
        self.texts: List[str] = []

    def text(self) -> str:
        return "\n".join(part for part in self.texts if part.strip())


def _decode_atom(kind: int, body: bytes) -> str:
    if kind == RT_TEXT_CHARS:
        return body.decode("utf-16-le", "replace")
    if kind == RT_TEXT_BYTES:
        return body.decode("cp1252", "replace")
    if kind == RT_CSTRING:
        return body.split(b"\x00", 1)[0].decode("cp1252", "replace")
    return ""


def _walk(payload: bytes, depth: int, slides: List[_Slide], current: Optional[_Slide]) -> None:
    if depth > _MAX_DEPTH:
        return
    offset = 0
    size = len(payload)
    while offset + 8 <= size:
        header = int.from_bytes(payload[offset:offset + 2], "little")
        kind = int.from_bytes(payload[offset + 2:offset + 4], "little")
        length = int.from_bytes(payload[offset + 4:offset + 8], "little")
        body_start = offset + 8
        body_end = body_start + length
        if body_end > size:
            # rekaman terpotong: pakai sisa data sebagai isi, jangan hentikan pembacaan
            body_end = size
        body = payload[body_start:body_end]
        version = header & 0x000F

        if kind in _CONTAINER_KINDS and length >= 0:
            if kind == RT_SLIDE:
                slide = _Slide(kind)
                slides.append(slide)
                _walk(body, depth + 1, slides, slide)
            else:
                _walk(body, depth + 1, slides, current)
        elif kind in (RT_TEXT_CHARS, RT_TEXT_BYTES, RT_CSTRING):
            text = _decode_atom(kind, body).strip()
            if text:
                if current is not None:
                    current.texts.append(text)
                elif slides:
                    slides[-1].texts.append(text)
                else:
                    placeholder = _Slide(version)
                    placeholder.texts.append(text)
                    slides.append(placeholder)
        offset = body_end


def read_presentation(content: bytes) -> Tuple[List[str], Dict[str, object]]:
    """Kembalikan (teks per slide, metadata)."""

    container = ole.OleContainer(content)
    if not container.has("PowerPoint Document"):
        raise AppError("INDEXING_FAILED", "stream PowerPoint Document tidak ada di berkas .ppt")
    payload = container.read("PowerPoint Document")
    if not payload:
        raise AppError("INDEXING_FAILED", "stream PowerPoint kosong")

    slides: List[_Slide] = []
    _walk(payload, 0, slides, None)
    pages = [slide.text() for slide in slides if slide.text().strip()]
    meta: Dict[str, object] = {
        "slides": len(pages),
        "streams": [name for name in container.names() if name],
        "bytes": len(payload),
    }
    return pages, meta


def text_of(pages: Sequence[str]) -> str:
    blocks: List[str] = []
    for number, text in enumerate(pages, start=1):
        body = text.strip()
        if body:
            blocks.append(f"Slide {number}\n{body}")
    return "\n\n".join(blocks)
