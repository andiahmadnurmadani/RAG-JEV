"""Klasifikasi potongan ber-CJK: dari ringkasan LLM, dokumen asli, atau teks sampah hasil parsing.

Menentukan perbaikan yang tepat: kalau sumbernya ringkasan LLM, filter di generator; kalau
sampah PDF, filter saat menyimpan; kalau dokumen memang berbahasa asing, jangan dirusak.
"""

from __future__ import annotations

import pickle
import re
import sqlite3
import sys
from collections import Counter

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
# Teks sampah: byte biner yang lolos jadi teks (PDF/arsip yang gagal di-parse).
GARBAGE = re.compile(r"[\uFFFD\x00-\x08\x0b\x0c\x0e-\x1f]")
PDF_LEFTOVER = re.compile(r"%PDF-\d|obj\s*<<|endobj|stream\s|FlateDecode")

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/st.sqlite"
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

kelas: Counter[str] = Counter()
contoh: dict[str, list] = {"ringkasan": [], "sampah": [], "dokumen": []}

for (blob,) in db.execute("select point from points"):
    try:
        point = pickle.loads(blob)
    except Exception:  # noqa: BLE001
        continue
    payload = dict(getattr(point, "payload", None) or {})
    content = str(payload.get("content") or "")
    if not content or not CJK.search(content):
        continue
    doc = str(payload.get("document_name") or payload.get("document_id") or "?")
    chunk_id = str(payload.get("chunk_id") or "")
    n_cjk = len(CJK.findall(content))
    n_garbage = len(GARBAGE.findall(content))
    rasio = n_cjk / max(len(content), 1)

    if chunk_id == "chunk_summary" or doc.lower().startswith("ringkasan"):
        kelas["ringkasan_llm"] += 1
        contoh["ringkasan"].append((doc, n_cjk, content[:130].replace("\n", " ")))
    elif n_garbage > 3 or PDF_LEFTOVER.search(content) or rasio > 0.05:
        kelas["teks_sampah"] += 1
        contoh["sampah"].append((doc, n_garbage, content[:130].replace("\n", " ")))
    else:
        kelas["dokumen_asli"] += 1
        contoh["dokumen"].append((doc, n_cjk, content[:130].replace("\n", " ")))

print("klasifikasi potongan ber-CJK:")
for name, count in kelas.most_common():
    print(f"  {name:16s} {count}")

for name, items in contoh.items():
    print(f"\n== {name} ({len(items)} contoh) ==")
    for doc, n, text in items[:6]:
        print(f"  [{n:3d} cjk] [{doc[:38]}] {text}")
