"""Pastikan detektor teks rusak BENAR-BENAR bekerja di image produksi.

"tidak ada kata rusak" hanya bermakna bila detektornya memang bisa menandai. Skrip ini
memberi detektor kalimat rusak buatan dan kalimat bersih: yang pertama harus ditandai,
yang kedua tidak.
"""

from __future__ import annotations

import sys

if "/srv" not in sys.path:
    sys.path.insert(0, "/srv")

from app.core.config import get_settings  # noqa: E402
from app.rag.generator import Generator  # noqa: E402

generator = Generator(get_settings())

RUSAK = [
    ("pemb.cgiian", "Data pemb.cgiian harus dicatat.", "pembagian"),
    ("hanyaLEMpar", "Nilai hanyaLEMpar dicatat.", "hanya"),
    ("praktikumaccording", "Kegiatan praktikumaccording selesai.", "praktikum menurut"),
    ("Tahunan2025", "Laporan Tahunan2025 sudah terbit.", "tahun 2025"),
]
BERSIH = [
    "Laporan Tahunan 2025 sudah diterbitkan oleh bank bjb.",
    "Gunakan knowledgeBase dan frontend untuk membangun antarmuka.",
    "Jenis remunerasi dan tanggal rapat dicatat dalam agenda.",
    "Kompas.com, ChatGPT, dan ESG adalah istilah yang sah.",
    "Dokumen PSL2025 adalah kode internal yang sah.",
]

gagal = 0
print("== kalimat rusak (harus DITANDAI) ==")
for label, kalimat, konteks in RUSAK:
    ditemukan = sorted(generator._corrupted_words(kalimat, konteks))
    tanda = "OK " if ditemukan else "LEWAT"
    if not ditemukan:
        gagal += 1
    print(f"  {tanda} {label:22s} -> {ditemukan}")

print("\n== kalimat bersih (TIDAK boleh ditandai) ==")
for kalimat in BERSIH:
    ditemukan = sorted(generator._corrupted_words(kalimat, kalimat))
    tanda = "OK " if not ditemukan else "POSITIF-PALSU"
    if ditemukan:
        gagal += 1
    print(f"  {tanda} {kalimat[:60]:60s} -> {ditemukan}")

print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} GAGAL")
raise SystemExit(0 if gagal == 0 else 1)
