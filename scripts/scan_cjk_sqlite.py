"""Pindai teks CJK pada SALINAN storage.sqlite (read-only, tanpa membuka klien Qdrant).

Qdrant embedded mengunci foldernya untuk proses aplikasi, jadi membuka klien kedua akan
mengganggu layanan. Berkas ini hanya membaca salinan sqlite dan membongkar kolom ``point``
(BLOB pickle PointStruct) - tidak menyentuh data yang dipakai aplikasi.
"""

from __future__ import annotations

import pickle
import re
import sqlite3
import sys

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/storage.sqlite"

db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
rows = db.execute("select point from points").fetchall()

total = 0
with_cjk = 0
examples: list[tuple[str, str]] = []
for (blob,) in rows:
    try:
        point = pickle.loads(blob)
    except Exception:  # noqa: BLE001
        continue
    payload = dict(getattr(point, "payload", None) or {})
    content = str(payload.get("content") or "")
    if not content:
        continue
    total += 1
    if CJK.search(content):
        with_cjk += 1
        if len(examples) < 12:
            examples.append((
                str(payload.get("document_name") or payload.get("document_id") or "?"),
                content[:170].replace("\n", " "),
            ))

print(f"berkas         : {path}")
print(f"total potongan : {total}")
print(f"mengandung CJK : {with_cjk}")
print(f"persentase     : {(with_cjk / total * 100) if total else 0:.2f}%")
if examples:
    print("\ncontoh:")
    for doc, text in examples:
        print(f"  [{doc[:40]}] {text}")
else:
    print("\ntidak ada potongan ber-CJK")
