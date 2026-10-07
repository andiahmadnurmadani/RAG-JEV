"""Buktikan filter aksara asing benar: membuang selipan model, TIDAK merusak fakta dokumen.

Kasus nyata dari produksi:
- Ringkasan model menyisipkan aksara Han: "Dokumen finals完整的 perakitan, wiring".
- Dokumen memuat aksara Han SAH sebagai label skema: "| 地 | Shield kabel, atau kosong |".
- Tabel Markdown harus tetap utuh setelah pembersihan.
"""

from __future__ import annotations

import os
import tempfile

os.environ.update({
    "APP_ENV": "test",
    "EMBEDDING_PROVIDER": "hash",
    "QDRANT_URL": "",
    "QDRANT_LOCAL_PATH": os.path.join(tempfile.mkdtemp(), "q"),
    "LLM_PROVIDER": "mock",
    "JEV_ENABLED": "false",
})

from app.core.config import get_settings  # noqa: E402
from app.parsing.sanitize import (  # noqa: E402
    clean_text,
    foreign_tokens,
    looks_like_binary_garbage,
    strip_foreign_tokens,
)
from app.rag.generator import Generator  # noqa: E402

gagal = 0
generator = Generator(get_settings())


def cek(label: str, hasil, harapan) -> None:
    global gagal
    ok = hasil == harapan
    if not ok:
        gagal += 1
    print(f"  {'OK  ' if ok else 'GAGAL'} {label}")
    if not ok:
        print(f"        dapat : {hasil!r}")
        print(f"        harap : {harapan!r}")


print("== 1. sampah biner dikenali ==")
dump_pdf = "%PDF-1.4\n5 0 obj <</Length 6 0 R/Filter /FlateDecode>> stream x\x9c\xed]y\u07bb" * 5
cek("dump PDF ditandai", looks_like_binary_garbage(dump_pdf)[0], True)
cek("teks normal lolos", looks_like_binary_garbage("Laporan tahunan bank bjb memuat total aset 100 triliun.")[0], False)
cek("teks dengan 1 karakter aneh lolos", looks_like_binary_garbage("Nilai aset 100 triliun \ufffd kecil.")[0], False)

print("\n== 2. clean_text tidak merusak isi ==")
cek("kontrol dibuang", clean_text("Halo\x00 dunia\x07."), "Halo dunia.")
cek("spasi dirapikan", clean_text("Nilai    aset\n\n\n\nbesar"), "Nilai aset\n\nbesar")
cek("tabel Markdown utuh", clean_text("| a | b |\n|---|---|\n| 1 | 2 |"), "| a | b |\n|---|---|\n| 1 | 2 |")

print("\n== 3. selipan model dibuang ==")
konteks = "Dokumen finals perakitan, wiring, dan firmware."
jawaban = "Dokumen finals完整的 perakitan, wiring, dan firmware selesai."
token = foreign_tokens(jawaban, allowed=[konteks])
cek("token asing terdeteksi", bool(token), True)
bersih = strip_foreign_tokens(jawaban, token)
cek("aksara Han hilang", "\u5b8c" in bersih or "\u6574" in bersih, False)
cek("kata Latin tetap", "perakitan" in bersih and "firmware" in bersih, True)

print("\n== 4. fakta dokumen TIDAK dirusak ==")
konteks_skema = "| \u5730 | Shield kabel, atau kosong |"
jawaban_skema = "| \u5730 | Shield kabel, atau kosong |"
token2 = foreign_tokens(jawaban_skema, allowed=[konteks_skema])
cek("aksara sah di konteks tidak dibuang", token2, [])
cek("baris skema utuh", strip_foreign_tokens(jawaban_skema, token2), jawaban_skema)

print("\n== 5. lewat Generator (jalur nyata) ==")
teks, dibuang = generator._strip_foreign_script(jawaban, konteks)
cek("Generator membuang selipan", "\u5b8c" in teks, False)
cek("Generator melaporkan apa yang dibuang", bool(dibuang), True)
teks2, dibuang2 = generator._strip_foreign_script(jawaban_skema, konteks_skema)
cek("Generator tidak menyentuh fakta", teks2, jawaban_skema)
cek("tidak ada yang dilaporkan", dibuang2, [])

print("\n== 6. tabel Markdown tetap utuh setelah dibersihkan ==")
jawaban_tabel = "## Pin\n\n| Pin | Ke |\n|---|---|\n| VCC | 3V3 |\n| \u5730 | GND |"
konteks_tabel = "| Pin | Ke |\n|---|---|\n| VCC | 3V3 |"
bersih_tabel = strip_foreign_tokens(jawaban_tabel, foreign_tokens(jawaban_tabel, allowed=[konteks_tabel]))
cek("garis pemisah tabel utuh", "|---|---|" in bersih_tabel, True)
cek("baris VCC utuh", "| VCC | 3V3 |" in bersih_tabel, True)

print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} GAGAL")
raise SystemExit(0 if gagal == 0 else 1)
