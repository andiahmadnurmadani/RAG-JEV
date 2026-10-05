"""Uji perilaku reranker leksikal: apakah urutannya benar-benar membaik?

Dijalankan langsung (tanpa server). Memakai kandidat tiruan dengan urutan fusi yang sengaja
buruk, lalu memeriksa apakah reranker menaikkan potongan yang relevan.
"""

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
from app.rag.reranker import LexicalReranker, NoopReranker, build_reranker  # noqa: E402

query = "berapa jatah cuti tahunan dan syarat pengajuannya"
# Urutan fusi sengaja menaruh yang paling tidak relevan di depan.
candidates = [
    "c1::Laporan keuangan triwulan memuat neraca dan laba rugi perusahaan.",
    "c2::Prosedur pengadaan barang dan jasa diatur dalam kebijakan tersendiri.",
    "c3::Jatah cuti tahunan adalah 12 hari kerja. Pengajuan cuti tahunan harus diajukan "
    "paling lambat 3 hari sebelum tanggal mulai.",
    "c4::Daftar hadir pegawai dan rekap absensi bulanan.",
]

print("== urutan MASUKAN (fusi) ==")
for index, item in enumerate(candidates, 1):
    print(f"  {index}. {item[:70]}")

print("\n== NoopReranker (perilaku LAMA saat provider=none) ==")
noop = NoopReranker()
noop_scores = noop.score(query, candidates)
noop_order = sorted(range(len(candidates)), key=lambda i: noop_scores[i], reverse=True)
for rank, index in enumerate(noop_order, 1):
    print(f"  {rank}. [{noop_scores[index]:.3f}] {candidates[index][:60]}")
print("  -> teratas:", candidates[noop_order[0]][:60])

print("\n== LexicalReranker (BARU) ==")
lexical = LexicalReranker()
lexical_scores = lexical.score(query, candidates)
lexical_order = sorted(range(len(candidates)), key=lambda i: lexical_scores[i], reverse=True)
for rank, index in enumerate(lexical_order, 1):
    print(f"  {rank}. [{lexical_scores[index]:.3f}] {candidates[index][:60]}")
print("  -> teratas:", candidates[lexical_order[0]][:60])

print("\n== penilaian ==")
lama_benar = noop_order[0] == 2
baru_benar = lexical_order[0] == 2
print(f"  peringkat-1 relevan (LAMA/noop) : {lama_benar}")
print(f"  peringkat-1 relevan (BARU/lexical): {baru_benar}")
print(f"  skor teratas dinormalkan ke 1.0   : {max(lexical_scores) == 1.0}")
# Threshold tidak boleh memotong kandidat terbaik.
print(f"  kandidat terbaik >= threshold 0.35: {max(lexical_scores) >= 0.35}")

# build_reranker harus menurunkan provider neural yang tak terpasang ke lexical.
settings = get_settings()
settings.reranker_fallback_to_lexical = True


def _simulate_missing(provider: str) -> bool:
    """Paksa pustaka neural dianggap tidak ada, untuk menguji jalur fallback."""
    import app.rag.reranker as module

    original = module._neural_available
    module._neural_available = lambda name: False if name == provider else original(name)
    try:
        return build_reranker(settings).name
    finally:
        module._neural_available = original


settings.reranker_provider = "fastembed"
print(f"\n  provider=fastembed -> {build_reranker(settings).name} (apa adanya di mesin ini)")
settings.reranker_provider = "sentence_transformers"
missing_name = _simulate_missing("sentence_transformers")
print(f"  provider=sentence_transformers (pustaka dianggap hilang) -> {missing_name}")

settings.reranker_provider = "lexical"
lexical_name = build_reranker(settings).name
print(f"  provider=lexical -> {lexical_name}")

gagal = 0
if not baru_benar:
    print("GAGAL: reranker leksikal tidak menaikkan kandidat relevan")
    gagal += 1
if max(lexical_scores) != 1.0:
    print("GAGAL: skor tidak dinormalkan ke 1.0")
    gagal += 1
if missing_name != "lexical":
    print("GAGAL: fallback ke lexical tidak bekerja saat pustaka neural tidak ada")
    gagal += 1
if lexical_name != "lexical":
    print("GAGAL: provider=lexical tidak menghasilkan LexicalReranker")
    gagal += 1
print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} GAGAL")
raise SystemExit(0 if gagal == 0 else 1)
