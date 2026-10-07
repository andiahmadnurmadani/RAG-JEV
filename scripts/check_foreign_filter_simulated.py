"""Simulasi model yang menyisipkan aksara China: buktikan filter memisahkan selipan dari fakta.

Ini uji paling penting, karena model nyata (space-bunny-free) TIDAK selalu menyisipkan aksara
China - jadi tanpa simulasi, "jawaban bersih" bisa berarti "model kebetulan tidak menyisipkan"
bukan "filter bekerja". Di sini keluaran model dipaksa memuat aksara China, lalu diperiksa:

1. aksara selipan dibuang;
2. kata Latin di sekitarnya tidak ikut hilang;
3. fakta dokumen yang MEMANG beraksara asing tetap utuh.
"""

from __future__ import annotations

import os
import tempfile

os.environ.update({
    "APP_ENV": "test",
    "EMBEDDING_PROVIDER": "hash",
    "QDRANT_URL": "",
    "QDRANT_LOCAL_PATH": os.path.join(tempfile.mkdtemp(), "q"),
    "LLM_PROVIDER": "openai_compatible",
    "LLM_BASE_URL": "http://contoh.invalid/v1",
    "LLM_API_KEY": "kunci-uji",
    "JEV_ENABLED": "false",
})

from app.core.config import get_settings  # noqa: E402
from app.rag.context import build_context  # noqa: E402
from app.rag.generator import LLMUsage, Generator  # noqa: E402
from app.rag.retriever import Candidate  # noqa: E402

settings = get_settings()
generator = Generator(settings)


class _FakeClient:
    """Model palsu yang mengembalikan jawaban persis seperti yang kita tentukan."""

    def __init__(self, jawaban: str) -> None:
        self.jawaban = jawaban
        self.model = "palsu"

    def chat(self, messages, **kwargs):  # noqa: ANN001
        return self.jawaban, LLMUsage(model="palsu", finish_reason="stop", output_tokens=50)

    def health(self) -> str:
        return "ok"


KONTEKS = (
    "Dokumen final perakitan memuat wiring, firmware, dan langkah pengujian. "
    "Setiap unit diuji selama 24 jam sebelum dikirim. Sensor mengukur kelembapan tanah."
)
CANDIDATE = Candidate(
    chunk_id="c1", document_id="d1", content=KONTEKS,
    document_name="spek.md", expanded=True, document_order=1,
)

gagal = 0


def cek(label: str, syarat: bool, detail: str = "") -> None:
    global gagal
    if not syarat:
        gagal += 1
    print(f"  {'OK  ' if syarat else 'GAGAL'} {label}{(' - ' + detail) if detail else ''}")


def jawab(keluaran_model: str) -> str:
    generator.rebind_llm_client(_FakeClient(keluaran_model))
    context = build_context([CANDIDATE], max_tokens=2000, max_chunks=1)
    return generator.answer(query="Apa isi dokumen final perakitan?", context=context).answer


print("== 1. aksara China yang diselipkan model dibuang ==")
keluaran = "Dokumen final perakitan完整的 memuat wiring dan firmware [1]."
hasil = jawab(keluaran)
print("   model :", keluaran)
print("   hasil :", hasil)
cek("aksara China hilang", "完" not in hasil and "整" not in hasil)
cek("kata Latin tetap ada", "perakitan" in hasil and "wiring" in hasil and "firmware" in hasil)
cek("sitasi tetap ada", "[1]" in hasil)

print("\n== 2. aksara China di tengah kalimat, banyak tempat ==")
keluaran2 = "Ringkasan: 文档 memuat 三个 komponen utama dan 步骤 pengujian [1]."
hasil2 = jawab(keluaran2)
print("   hasil :", hasil2)
cek("semua aksara China hilang", not any("\u4e00" <= c <= "\u9fff" for c in hasil2))
cek("kata Indonesia tetap", "memuat" in hasil2 and "komponen" in hasil2 and "pengujian" in hasil2)

print("\n== 3. tabel Markdown tidak rusak oleh pembersihan ==")
keluaran3 = "| Komponen | Jumlah |\n|---|---|\n| Sensor 传感器 | 1 |\n| Modul LoRa | 1 |"
hasil3 = jawab(keluaran3)
print("   hasil :\n     " + hasil3.replace("\n", "\n     "))
cek("garis pemisah tabel utuh", "|---|---|" in hasil3)
cek("baris Modul LoRa utuh", "Modul LoRa" in hasil3)
cek("aksara China hilang", not any("\u4e00" <= c <= "\u9fff" for c in hasil3))

print("\n== 4. fakta dokumen beraksara asing TIDAK dirusak ==")
# Dokumen sumber memuat aksara Han sebagai label skema; jawaban yang menyalinnya harus utuh.
KONTEKS_SKEMA = "| 地 | Shield kabel, atau kosong |\n| VCC | 3V3 |"
cand = Candidate(chunk_id="c2", document_id="d2", content=KONTEKS_SKEMA,
                 document_name="skema.md", expanded=True, document_order=1)
generator.rebind_llm_client(_FakeClient("| 地 | Shield kabel |\n| VCC | 3V3 |"))
ctx = build_context([cand], max_tokens=2000, max_chunks=1)
hasil4 = generator.answer(query="Apa isi tabel skema?", context=ctx).answer
print("   hasil :", hasil4.replace("\n", " / "))
cek("aksara sah dipertahankan", "地" in hasil4)

print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} GAGAL")
raise SystemExit(0 if gagal == 0 else 1)
