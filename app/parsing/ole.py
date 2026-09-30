"""Pembaca kontainer OLE2 / CFB (Compound File Binary) memakai pustaka standar.

Format lama Microsoft Office (.doc, .xls, .ppt) bukan arsip zip seperti versi barunya,
melainkan satu "berkas di dalam berkas": sebuah kontainer bersektor yang berisi
beberapa stream bernama (mis. ``WordDocument``, ``Workbook``, ``PowerPoint Document``).
Modul ini membaca kontainer itu saja - tidak tahu apa pun soal Word/Excel/PowerPoint -
supaya tiap format bisa punya pembacanya sendiri di atas lapisan yang sudah teruji.

Semua yang dibutuhkan ada di pustaka standar: ``struct`` dan iterasi byte. Tidak ada
``olefile``/``xlrd``/``textract``, jadi berkas lama tetap bisa dibaca di mesin kecil
sekaligus tetap jalan di lingkungan tanpa izin memasang paket.
"""

from __future__ import annotations

import struct
from typing import Dict, Iterator, List, Optional

from app.core.errors import AppError

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC

TYPE_STORAGE = 1
TYPE_STREAM = 2
TYPE_ROOT = 5

_HEADER_SIZE = 512
_DIRECTORY_ENTRY_SIZE = 128
_MAX_CHAIN_STEPS = 1 << 20  # pagar supaya rantai rusak tidak bikin program berputar


class OleContainer:
    """Kontainer OLE2: daftar stream + pembacaan per stream."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        if len(data) < _HEADER_SIZE or not data.startswith(MAGIC):
            raise AppError("INDEXING_FAILED", "bukan berkas OLE2/CFB yang sah")
        self._sector_size = 1 << self._u16(0x1E)
        self._mini_sector_size = 1 << self._u16(0x20)
        if self._sector_size not in (512, 4096) or self._mini_sector_size != 64:
            raise AppError("INDEXING_FAILED", "ukuran sektor OLE2 tidak dikenal")
        self._mini_cutoff = self._u32(0x38) or 4096
        self._fat = self._read_fat()
        self._mini_fat = self._read_mini_fat()
        self._entries = self._read_directory()
        self._mini_stream = b""

    # ------------------------------------------------------------------ #
    # Pembacaan header
    # ------------------------------------------------------------------ #
    def _u16(self, offset: int) -> int:
        return struct.unpack_from("<H", self._data, offset)[0]

    def _u32(self, offset: int) -> int:
        return struct.unpack_from("<I", self._data, offset)[0]

    def _sector(self, index: int) -> bytes:
        start = _HEADER_SIZE + index * self._sector_size
        end = start + self._sector_size
        if index < 0 or end > len(self._data):
            return b""
        return self._data[start:end]

    def _read_fat(self) -> List[int]:
        fat_sectors: List[int] = []
        for index in range(109):
            sector = self._u32(0x4C + index * 4)
            if sector in (FREESECT, ENDOFCHAIN):
                break
            fat_sectors.append(sector)
        # DIFAT lanjutan (berkas besar): tiap sektor DIFAT memuat (n-1) entri + penunjuk berikutnya
        next_difat = self._u32(0x44)
        per_sector = self._sector_size // 4 - 1
        guard = 0
        while next_difat not in (FREESECT, ENDOFCHAIN) and guard < _MAX_CHAIN_STEPS:
            guard += 1
            block = self._sector(next_difat)
            if not block:
                break
            values = struct.unpack_from("<%dI" % (self._sector_size // 4), block, 0)
            for value in values[:per_sector]:
                if value in (FREESECT, ENDOFCHAIN):
                    break
                fat_sectors.append(value)
            next_difat = values[per_sector]

        entries: List[int] = []
        for sector in fat_sectors:
            block = self._sector(sector)
            if not block:
                continue
            entries.extend(struct.unpack_from("<%dI" % (self._sector_size // 4), block, 0))
        return entries

    def _chain(self, start: int) -> Iterator[int]:
        """Ikuti rantai FAT dari satu sektor sampai ujungnya."""
        current = start
        steps = 0
        while current < ENDOFCHAIN and steps < _MAX_CHAIN_STEPS:
            yield current
            steps += 1
            if current >= len(self._fat):
                return
            current = self._fat[current]

    def _read_chain(self, start: int, size: Optional[int] = None) -> bytes:
        chunks = [self._sector(index) for index in self._chain(start)]
        blob = b"".join(chunks)
        return blob[:size] if size is not None else blob

    def _read_mini_fat(self) -> List[int]:
        first = self._u32(0x3C)
        count = self._u32(0x40)
        if first in (FREESECT, ENDOFCHAIN) or count == 0:
            return []
        entries: List[int] = []
        for sector in self._chain(first):
            block = self._sector(sector)
            if not block:
                break
            entries.extend(struct.unpack_from("<%dI" % (self._sector_size // 4), block, 0))
            if len(entries) >= count * (self._sector_size // 4):
                break
        return entries

    def _read_directory(self) -> List[Dict[str, object]]:
        first = self._u32(0x30)
        blob = self._read_chain(first)
        entries: List[Dict[str, object]] = []
        for offset in range(0, len(blob) - _DIRECTORY_ENTRY_SIZE + 1, _DIRECTORY_ENTRY_SIZE):
            chunk = blob[offset:offset + _DIRECTORY_ENTRY_SIZE]
            name_length = struct.unpack_from("<H", chunk, 0x40)[0]
            name = chunk[: max(0, name_length - 2)].decode("utf-16-le", "ignore") if name_length >= 2 else ""
            entries.append({
                "name": name,
                "type": chunk[0x42],
                "start": struct.unpack_from("<I", chunk, 0x74)[0],
                "size": struct.unpack_from("<Q", chunk, 0x78)[0],
            })
        return entries

    # ------------------------------------------------------------------ #
    # API publik
    # ------------------------------------------------------------------ #
    def names(self) -> List[str]:
        return [str(entry["name"]) for entry in self._entries if entry["type"] == TYPE_STREAM]

    def has(self, name: str) -> bool:
        return self._find(name) is not None

    def _find(self, name: str) -> Optional[Dict[str, object]]:
        wanted = name.lower()
        for entry in self._entries:
            if entry["type"] == TYPE_STREAM and str(entry["name"]).lower() == wanted:
                return entry
        return None

    def _root(self) -> Optional[Dict[str, object]]:
        for entry in self._entries:
            if entry["type"] == TYPE_ROOT:
                return entry
        return None

    def _mini_stream_data(self) -> bytes:
        if self._mini_stream:
            return self._mini_stream
        root = self._root()
        if root is None:
            return b""
        size = int(root["size"] or 0)
        if not size:
            return b""
        self._mini_stream = self._read_chain(int(root["start"]), size)
        return self._mini_stream

    def read(self, name: str) -> bytes:
        """Isi satu stream bernama (case-insensitive)."""
        entry = self._find(name)
        if entry is None:
            raise AppError("INDEXING_FAILED", f"stream {name!r} tidak ada di berkas OLE2")
        size = int(entry["size"] or 0)
        start = int(entry["start"] or 0)
        if size == 0:
            return b""
        if size < self._mini_cutoff:
            blob = self._mini_stream_data()
            out = bytearray()
            current = start
            steps = 0
            while current < ENDOFCHAIN and steps < _MAX_CHAIN_STEPS:
                offset = current * self._mini_sector_size
                out += blob[offset: offset + self._mini_sector_size]
                steps += 1
                if current >= len(self._mini_fat):
                    break
                current = self._mini_fat[current]
            return bytes(out[:size])
        return self._read_chain(start, size)

    def read_optional(self, name: str) -> bytes:
        return self.read(name) if self.has(name) else b""


def is_ole(data: bytes) -> bool:
    return data[:8] == MAGIC


def open_ole(data: bytes) -> OleContainer:
    return OleContainer(data)
