"""Uji perilaku detektor teks rusak (dijalankan langsung, tanpa server)."""

from __future__ import annotations

import os
import tempfile

os.environ.update({
    "APP_ENV": "test",
    "LLM_PROVIDER": "mock",
    "EMBEDDING_PROVIDER": "hash",
    "QDRANT_URL": "",
    "JEV_ENABLED": "false",
    "QDRANT_LOCAL_PATH": os.path.join(tempfile.mkdtemp(), "q"),
})

from app.core.config import get_settings  # noqa: E402
from app.rag.generator import Generator  # noqa: E402

generator = Generator(get_settings())

KONTEKS = """
[DOCUMENT] Name: modul sqlite
[CONTENT]
MODUL 5 UPDATE DAN DELETE PADA SQLite
Tujuan praktikum: mahasiswa memahami operasi UPDATE dan DELETE pada basis data SQLite.
Kolom employee_id, jenis_cuti, dan tanggal_mulai wajib diisi di sistem.
Jatah cuti tahunan adalah 12 hari kerja per tahun. Cuti sakit 14 hari.
Pembagian materi dilakukan per pertemuan. Praktikum dilakukan di laboratorium.
"""

# (teks, apakah HARUS dianggap rusak)
KASUS = [
    # Kata rusak nyata dari keluhan
    ("Pembagian materi dilakukan per pertemuan. pemb.cgiian materi praktikum.", True),
    ("Materi hanyaLEMpar ke mahasiswa.", True),
    ("Praktikumaccording dengan modul SQLite.", True),
    # Teks normal Indonesia - TIDAK boleh ditandai
    ("Pembagian materi dilakukan per pertemuan sesuai jadwal laboratorium.", False),
    ("Tujuan praktikum adalah memahami operasi UPDATE dan DELETE pada SQLite.", False),
    ("Kolom employee_id wajib diisi di sistem informasi.", False),
    ("Jatah cuti tahunan adalah 12 hari kerja per tahun.", False),
    ("Mahasiswa memahami instruksi dan mengerjakan latihan dengan teliti.", False),
    ("Kegiatan praktikum berlangsung di laboratorium komputer.", False),
    # Teks normal Inggris - TIDAK boleh ditandai
    ("The update and delete operations on SQLite are demonstrated in class.", False),
    ("Students must complete the assignment before the deadline.", False),
    # Kata dari konteks walau tidak umum - TIDAK boleh ditandai
    ("jenis_cuti dan tanggal_mulai wajib diisi.", False),
    # Istilah teknis umum - TIDAK boleh ditandai
    ("Gunakan knowledgeBase dan frontend untuk membangun antarmuka.", False),
]

print(f"{'HASIL':8s} {'HARAP':8s} KALIMAT")
gagal = 0
for teks, harus_rusak in KASUS:
    rusak = generator._corrupted_words(teks, KONTEKS)
    terdeteksi = bool(rusak)
    ok = terdeteksi == harus_rusak
    if not ok:
        gagal += 1
    tanda = "OK" if ok else "GAGAL"
    print(f"{tanda:8s} {'rusak' if harus_rusak else 'bersih':8s} {teks[:64]}")
    if rusak:
        print(f"         -> terdeteksi: {sorted(rusak)}")

print()
print("SEMUA LULUS" if gagal == 0 else f"{gagal} GAGAL dari {len(KASUS)}")
raise SystemExit(0 if gagal == 0 else 1)
