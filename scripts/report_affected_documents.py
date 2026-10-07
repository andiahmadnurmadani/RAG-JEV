"""Daftar dokumen terdampak di produksi: sampah biner dan ringkasan beraksara asing.

Read-only. Membaca SALINAN storage.sqlite supaya tidak mengganggu aplikasi (Qdrant embedded
mengunci foldernya; klien kedua akan membuat layanan gagal membuka data).

Keluaran: per dokumen, berapa potongan sampah, berapa ringkasan beraksara asing, dan berapa
potongan sah beraksara asing (yang TIDAK boleh disentuh).
"""

from __future__ import annotations

import pickle
import re
import sqlite3
import sys
from collections import defaultdict

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
GARBAGE = re.compile(r"[\uFFFD\x00-\x08\x0b\x0c\x0e-\x1f]")
PDF_LEFTOVER = re.compile(r"%PDF-\d|obj\s*<<|endobj|endstream|/FlateDecode")

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/st.sqlite"
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

stat: dict[str, dict] = defaultdict(lambda: {"total": 0, "sampah": 0, "ringkasan_asing": 0, "sah_asing": 0, "kb": set()})

for (blob,) in db.execute("select point from points"):
    try:
        point = pickle.loads(blob)
    except Exception:  # noqa: BLE001
        continue
    payload = dict(getattr(point, "payload", None) or {})
    content = str(payload.get("content") or "")
    if not content:
        continue
    doc = str(payload.get("document_name") or payload.get("document_id") or "?")
    chunk_id = str(payload.get("chunk_id") or "")
    entry = stat[doc]
    entry["total"] += 1
    entry["kb"].add(str(payload.get("knowledge_base_id") or "-"))

    n_garbage = len(GARBAGE.findall(content))
    markers = len(PDF_LEFTOVER.findall(content))
    rasio_cjk = len(CJK.findall(content)) / max(len(content), 1)
    is_summary = chunk_id == "chunk_summary"

    if n_garbage > 3 or markers >= 4 or rasio_cjk > 0.05:
        entry["sampah"] += 1
    elif is_summary and CJK.search(content):
        entry["ringkasan_asing"] += 1
    elif CJK.search(content):
        entry["sah_asing"] += 1

print(f"{'dokumen':52s} {'total':>5s} {'sampah':>6s} {'ringkasan':>9s} {'sah':>4s}  kb")
print("-" * 96)
terdampak = []
for doc, e in sorted(stat.items(), key=lambda kv: -(kv[1]["sampah"] + kv[1]["ringkasan_asing"])):
    if e["sampah"] or e["ringkasan_asing"]:
        terdampak.append((doc, e))
        print(f"{doc[:52]:52s} {e['total']:5d} {e['sampah']:6d} {e['ringkasan_asing']:9d} {e['sah_asing']:4d}  {','.join(sorted(e['kb']))[:20]}")

print("-" * 96)
print(f"total potongan        : {sum(e['total'] for e in stat.values())}")
print(f"dokumen terdampak     : {len(terdampak)}")
print(f"potongan sampah       : {sum(e['sampah'] for _, e in terdampak)}")
print(f"ringkasan beraksara   : {sum(e['ringkasan_asing'] for _, e in terdampak)}")
print(f"potongan SAH beraksara: {sum(e['sah_asing'] for e in stat.values())} (JANGAN disentuh)")
