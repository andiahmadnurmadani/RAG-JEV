"""Membuat contoh berkas Office **lama** (.xls/.doc/.ppt) untuk pengujian.

Kenapa berkas dibuat sendiri: di mesin ini tidak ada LibreOffice/MS Office, dan tidak ada
contoh .doc/.xls/.ppt di disk. Supaya pengujiannya tetap punya nilai:

* ``.xls`` ditulis oleh **xlwt** (penulis BIFF8 pihak ketiga) dan dibaca ulang oleh **xlrd**
  (pembaca BIFF8 pihak ketiga) sebagai acuan - jadi bukan penulis/pembaca buatan sendiri;
* ``.doc`` dan ``.ppt`` memakai kontainer CFB dari penulis kecil di dalam berkas ini;
  kontainernya **diperiksa olefile** (pustaka pihak ketiga) sebagai pembanding, sedangkan
  isi stream-nya disusun menurut spesifikasi biner Word 97 (FIB + CLX piece table) dan
  PowerPoint 97 (record container/atom). Isi stream itu memang buatan sendiri dan
  disebutkan apa adanya di laporan.

Jalankan dengan venv yang punya olefile/xlwt/xlrd (bukan venv layanan):

    data/tmp/olevenv/Scripts/python.exe scripts/make_legacy_samples.py data/tmp/legacy
"""

from __future__ import annotations

import datetime as dt
import struct
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import xlwt

MARK_DOC = "DOC2026MARK"
MARK_PPT = "PPT2026MARK"


# --------------------------------------------------------------------------- #
# Penulis kontainer CFB (OLE2) kecil - bahan uji saja
# --------------------------------------------------------------------------- #

SECTOR = 512
MINI_SECTOR = 64
MINI_CUTOFF = 4096
FREE, END, FATS = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF
MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _entry(name: str, kind: int, start: int, size: int, *, right: int = NOSTREAM,
           child: int = NOSTREAM) -> bytes:
    raw = (name.encode("utf-16-le") + b"\x00\x00")[:64]
    block = bytearray(128)
    block[0:64] = raw.ljust(64, b"\x00")
    struct.pack_into("<H", block, 0x40, len(raw))
    block[0x42] = kind
    block[0x43] = 1                       # simpul "hitam"
    struct.pack_into("<I", block, 0x44, NOSTREAM)
    struct.pack_into("<I", block, 0x48, right)
    struct.pack_into("<I", block, 0x4C, child)
    struct.pack_into("<I", block, 0x74, start)
    struct.pack_into("<Q", block, 0x78, size)
    return bytes(block)


def write_container(path: Path, streams: List[Tuple[str, bytes]]) -> None:
    """Tulis kontainer CFB v3: stream < 4096 byte masuk mini stream."""

    small = [(name, data) for name, data in streams if len(data) < MINI_CUTOFF]
    large = [(name, data) for name, data in streams if len(data) >= MINI_CUTOFF]

    mini_payload = bytearray()
    mini_fat: List[int] = []
    mini_start: Dict[str, int] = {}
    cursor = 0
    for name, data in small:
        pieces = max(1, (len(data) + MINI_SECTOR - 1) // MINI_SECTOR)
        stack = cursor // MINI_SECTOR
        mini_start[name] = stack
        mini_payload += data.ljust(pieces * MINI_SECTOR, b"\x00")
        for index in range(pieces):
            mini_fat.append(stack + index + 1 if index < pieces - 1 else END)
        cursor += pieces * MINI_SECTOR
    mini_stream = bytes(mini_payload)

    large_payload = bytearray()
    large_chain: Dict[str, List[int]] = {}
    used = 0
    for name, data in large:
        pieces = max(1, (len(data) + SECTOR - 1) // SECTOR)
        large_payload += data.ljust(pieces * SECTOR, b"\x00")
        large_chain[name] = list(range(used, used + pieces))
        used += pieces

    mini_sectors = (len(mini_stream) + SECTOR - 1) // SECTOR
    order = [name for name, _ in small] + [name for name, _ in large]
    dir_sectors = max(1, ((1 + len(order)) * 128 + SECTOR - 1) // SECTOR)
    mini_fat_sectors = (len(mini_fat) * 4 + SECTOR - 1) // SECTOR

    fat_sectors = 1
    while True:
        data_sectors = dir_sectors + mini_fat_sectors + mini_sectors + used
        need = max(1, ((data_sectors + fat_sectors) * 4 + SECTOR - 1) // SECTOR)
        if need == fat_sectors:
            break
        fat_sectors = need

    dir_start = fat_sectors
    mini_fat_start = dir_start + dir_sectors if mini_fat_sectors else END
    mini_stream_start = (mini_fat_start + mini_fat_sectors) if mini_sectors else END
    big_start = (mini_stream_start + mini_sectors) if used else END
    total = dir_start + dir_sectors + mini_fat_sectors + mini_sectors + used

    fat = [FREE] * total
    for index in range(fat_sectors):
        fat[index] = FATS
    for offset in range(dir_sectors):
        fat[dir_start + offset] = dir_start + offset + 1 if offset < dir_sectors - 1 else END
    for offset in range(mini_fat_sectors):
        fat[mini_fat_start + offset] = (mini_fat_start + offset + 1
                                       if offset < mini_fat_sectors - 1 else END)
    for offset in range(mini_sectors):
        fat[mini_stream_start + offset] = (mini_stream_start + offset + 1
                                          if offset < mini_sectors - 1 else END)
    for chain in large_chain.values():
        for offset, index in enumerate(chain):
            fat[big_start + index] = big_start + chain[offset + 1] if offset < len(chain) - 1 else END

    directory = bytearray()
    directory += _entry("Root Entry", 5, mini_stream_start if mini_sectors else END,
                        len(mini_stream), child=1 if order else NOSTREAM)
    lookup = dict(streams)
    for position, name in enumerate(order):
        start = mini_start[name] if name in mini_start else big_start + large_chain[name][0]
        right = position + 2 if position + 1 < len(order) else NOSTREAM
        directory += _entry(name, 2, start, len(lookup[name]), right=right)

    fat_full = fat + [FREE] * (fat_sectors * 128 - len(fat))
    body = bytearray()
    for index in range(fat_sectors):
        body += struct.pack("<128I", *fat_full[index * 128:(index + 1) * 128])
    body += bytes(directory).ljust(dir_sectors * SECTOR, b"\x00")
    if mini_fat_sectors:
        body += struct.pack("<%dI" % len(mini_fat), *mini_fat).ljust(mini_fat_sectors * SECTOR, b"\x00")
    if mini_sectors:
        body += mini_stream.ljust(mini_sectors * SECTOR, b"\x00")
    if used:
        body += bytes(large_payload)

    header = bytearray(512)
    header[0:8] = MAGIC
    struct.pack_into("<H", header, 0x18, 0x003E)
    struct.pack_into("<H", header, 0x1A, 0x0003)
    struct.pack_into("<H", header, 0x1C, 0xFFFE)
    struct.pack_into("<H", header, 0x1E, 9)      # 512 byte/sektor
    struct.pack_into("<H", header, 0x20, 6)      # 64 byte/mini sektor
    struct.pack_into("<I", header, 0x2C, fat_sectors)
    struct.pack_into("<I", header, 0x30, dir_start)
    struct.pack_into("<I", header, 0x38, MINI_CUTOFF)
    struct.pack_into("<I", header, 0x3C, mini_fat_start)
    struct.pack_into("<I", header, 0x40, mini_fat_sectors)
    struct.pack_into("<I", header, 0x44, END)
    struct.pack_into("<I", header, 0x48, 0)
    for index in range(109):
        struct.pack_into("<I", header, 0x4C + index * 4, index if index < fat_sectors else FREE)
    path.write_bytes(bytes(header) + bytes(body))


# --------------------------------------------------------------------------- #
# .xls  (ditulis xlwt -> BIFF8 sungguhan)
# --------------------------------------------------------------------------- #


def write_xls(target: Path) -> None:
    book = xlwt.Workbook(encoding="utf-8")

    date_style = xlwt.XFStyle()
    date_style.num_format_str = "YYYY-MM-DD"
    money_style = xlwt.XFStyle()
    money_style.num_format_str = "#,##0"

    sheet = book.add_sheet("Penjualan")
    headers = ["Tanggal", "Kode", "Produk", "Jumlah", "Harga Satuan", "Total"]
    for column, title in enumerate(headers):
        sheet.write(0, column, title)

    rows = [
        (dt.datetime(2026, 8, 2), "PRD-001", "Kopi Susu Gula Aren", 30, 18000, 540000),
        (dt.datetime(2026, 8, 5), "PRD-003", "Ayam Geprek", 25, 25000, 625000),
        (dt.datetime(2026, 8, 9), "PRD-003", "Ayam Geprek", 42, 25000, 1050000),
        (dt.datetime(2026, 8, 14), "PRD-002", "Es Teh Manis", 42, 8000, 336000),
        (dt.datetime(2026, 8, 21), "PRD-006", "Roti Bakar Keju", 18, 20000, 360000),
    ]
    for offset, row in enumerate(rows, start=1):
        sheet.write(offset, 0, row[0], date_style)
        sheet.write(offset, 1, row[1])
        sheet.write(offset, 2, row[2])
        sheet.write(offset, 3, row[3])
        sheet.write(offset, 4, row[4], money_style)
        sheet.write(offset, 5, row[5], money_style)
    # sel kosong di tengah + teks rupiah supaya pemisah ribuan ikut teruji
    sheet.write(6, 0, "Catatan")
    sheet.write(6, 1, MARK_DOC.replace("DOC", "XLS"))
    sheet.write(6, 3, "Rp1.500.000")

    summary = book.add_sheet("Ringkasan")
    summary.write(0, 0, "Keterangan")
    summary.write(0, 1, "Nilai")
    summary.write(1, 0, "Periode")
    summary.write(1, 1, "Agustus 2026")
    summary.write(2, 0, "Cabang")
    summary.write(2, 1, "Kolab Cabang Bandung")

    book.save(str(target))


# --------------------------------------------------------------------------- #
# .doc  (kontainer olefile + struktur Word 97)
# --------------------------------------------------------------------------- #
def _fib(fc_min: int, fc_mac: int, *, complex_table: bool = False, fc_clx: int = 0, lcb_clx: int = 0,
         ccp_text: int = 0, table_name: str = "") -> bytes:
    """FIB Word 97 (nFib=193) dengan bagian yang benar-benar dibaca pembaca kita."""
    block = bytearray(1024)
    struct.pack_into("<H", block, 0x0000, 0xA5EC)          # wIdent
    struct.pack_into("<H", block, 0x0002, 0x00C1)          # nFib 193
    struct.pack_into("<H", block, 0x000A, 0x0004 if complex_table else 0x0000)  # fComplex
    struct.pack_into("<i", block, 0x0018, fc_min)          # fcMin
    struct.pack_into("<i", block, 0x001C, fc_mac)          # fcMac
    struct.pack_into("<i", block, 0x004C, ccp_text)        # ccpText
    if complex_table:
        struct.pack_into("<i", block, 0x01A2, fc_clx)      # fcClx
        struct.pack_into("<i", block, 0x01A6, lcb_clx)     # lcbClx
        struct.pack_into("<H", block, 0x000A, 0x0204)      # fWhichTblStm=1 + fComplex
    return bytes(block)


def write_doc_simple(target: Path) -> None:
    """.doc tanpa piece table: teks ANSI utuh di WordDocument (fComplex=0)."""
    paragraphs = [
        f"SOP Cuti 2026 {MARK_DOC}",
        "1. Kuota cuti tahunan karyawan tetap adalah 12 hari kerja.",
        "2. Pengajuan dilakukan lewat sistem HR paling lambat 3 hari sebelum tanggal.",
        "3. Sisa cuti hangus pada 31 Desember; café tetap buka 08.00-17.00.",
        "",
        "Disahkan oleh: Divisi SDM",
    ]
    text = "\r".join(paragraphs).encode("cp1252", "replace")
    body_offset = 2048
    fib = _fib(body_offset, body_offset + len(text))
    stream = fib + b"\x00" * (body_offset - len(fib)) + text
    write_container(target, [("WordDocument", stream)])


def write_doc_pieces(target: Path) -> None:
    """.doc dengan piece table: dua potongan, satu ANSI satu UTF-16 (fComplex=1)."""
    part_ansi = f"Bagian pertama: {MARK_DOC} - teks ANSI satu byte per karakter.\r"
    part_wide = "Bagian kedua: teks Unicode – tandai dengan é, ü, dan ½.\r"
    ansi = part_ansi.encode("cp1252")
    wide = part_wide.encode("utf-16-le")

    doc_offset_ansi = 2048
    doc_offset_wide = doc_offset_ansi + len(ansi)
    total_chars = len(part_ansi) + len(part_wide)

    # piece table (PlcPcd): 1 byte 0x02 + lcb + (n+1)*4 CP + n*8 PCD
    cp_ansi = 0
    cp_wide = len(part_ansi)
    clx_body = b"".join([
        struct.pack("<I", cp_ansi), struct.pack("<I", cp_wide), struct.pack("<I", total_chars),
    ])
    fc_ansi = doc_offset_ansi * 2 | 0x40000000   # fCompressed (bit 30) -> 1 byte/char
    fc_wide = doc_offset_wide                      # UTF-16, offset asli
    # PCD = 2 byte flag + 4 byte fc + 2 byte prm (8 byte), bukan 6
    clx_body += struct.pack("<H", 0) + struct.pack("<I", fc_ansi) + struct.pack("<H", 0)
    clx_body += struct.pack("<H", 0) + struct.pack("<I", fc_wide) + struct.pack("<H", 0)
    clx = b"\x02" + struct.pack("<I", len(clx_body)) + clx_body

    table_offset = 4096
    document = bytearray(doc_offset_wide + len(wide))
    document[doc_offset_ansi:doc_offset_ansi + len(ansi)] = ansi
    document[doc_offset_wide:doc_offset_wide + len(wide)] = wide
    fib = _fib(doc_offset_ansi, doc_offset_ansi + len(ansi), complex_table=True,
               fc_clx=table_offset, lcb_clx=len(clx), ccp_text=total_chars)
    document[:len(fib)] = fib

    write_container(target, [("WordDocument", bytes(document)),
                             ("1Table", b"\x00" * table_offset + clx)])


# --------------------------------------------------------------------------- #
# .ppt  (kontainer olefile + record PowerPoint 97)
# --------------------------------------------------------------------------- #
RT_DOCUMENT = 1000
RT_SLIDE = 1006
RT_TEXT_HEADER = 3999
RT_TEXT_CHARS = 4000
RT_TEXT_BYTES = 4008
RT_CSTRING = 4026


def _record(kind: int, body: bytes, instance: int = 0, container: bool = False) -> bytes:
    version = 0xF if container else 0x0
    head = struct.pack("<HHI", (instance << 4) | version, kind, len(body))
    return head + body


def write_ppt(target: Path) -> None:
    slides = [
        [f"Presentasi Tahunan {MARK_PPT}", "Ringkasan penjualan 2026", "Dibuat oleh Divisi Penjualan"],
        ["Target kuartal", "Naikkan penjualan 20 persen", "Fokus produk Ayam Geprek"],
    ]
    slide_records = []
    for lines in slides:
        body = _record(RT_TEXT_HEADER, struct.pack("<I", 0))
        for index, line in enumerate(lines):
            atom = _record(RT_TEXT_BYTES, line.encode("cp1252", "replace"))
            body += atom
            if index == 0:
                body += _record(RT_CSTRING, b"Judul slide\x00")
        slide_records.append(_record(RT_SLIDE, body, instance=0, container=True))

    document = _record(RT_DOCUMENT, b"".join(slide_records), instance=0, container=True)
    write_container(target, [("PowerPoint Document", document)])


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "data/tmp/legacy")
    out.mkdir(parents=True, exist_ok=True)
    write_xls(out / "laporan_penjualan_lama.xls")
    write_doc_simple(out / "sop_cuti_lama.doc")
    write_doc_pieces(out / "sop_cuti_potongan.doc")
    write_ppt(out / "presentasi_lama.ppt")
    # .doc yang sebenarnya RTF / HTML / OOXML - kasus nyata yang sering bikin teks hilang
    (out / "lpj_rtf.doc").write_bytes(
        b"{\\rtf1\\ansi\\deff0 {\\fonttbl{\\f0 Times;}} " + MARK_DOC.encode() +
        b" Laporan disimpan sebagai RTF walau berekstensi .doc.\\par}"
    )
    (out / "catatan_html.doc").write_bytes(
        b"<html><head><title>Catatan</title></head><body><h1>" + MARK_DOC.encode() +
        b"</h1><p>Berkas ini HTML walau bernama .doc.</p></body></html>"
    )
    for path in sorted(out.iterdir()):
        print(f"{path.name:32s} {path.stat().st_size:8d} byte")


if __name__ == "__main__":
    main()
