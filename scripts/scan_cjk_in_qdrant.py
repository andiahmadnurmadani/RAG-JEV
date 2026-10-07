"""Pindai payload Qdrant: apakah ada teks CJK (Han/Hiragana/Katakana/Hangul) di data nyata.

Read-only. Dijalankan di dalam container. Menjawab pertanyaan: apakah masalahnya di data yang
tersimpan (knowledge tercemar) atau di keluaran model.
"""

from __future__ import annotations

import re
import sys

if "/srv" not in sys.path:
    sys.path.insert(0, "/srv")

from app.core.config import get_settings  # noqa: E402
from app.qdrant.client import build_client  # noqa: E402

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
settings = get_settings()
client = build_client(settings)
collection = settings.qdrant_collection

points, offset = client.scroll(collection_name=collection, limit=512, with_payload=True, with_vectors=False, offset=None)
total = 0
with_cjk = 0
examples: list[tuple[str, str, str]] = []
while True:
    for point in points:
        payload = dict(point.payload or {})
        content = str(payload.get("content") or "")
        total += 1
        hits = CJK.findall(content)
        if hits:
            with_cjk += 1
            if len(examples) < 12:
                examples.append((
                    str(payload.get("document_name") or payload.get("document_id") or "?"),
                    str(payload.get("chunk_id") or "")[:14],
                    content[:150].replace("\n", " "),
                ))
    offset = getattr(points, "next_page_offset", None)
    if not offset:
        break
    points = client.scroll(collection_name=collection, limit=512, with_payload=True, with_vectors=False, offset=offset)

print(f"koleksi        : {collection}")
print(f"total potongan : {total}")
print(f"mengandung CJK : {with_cjk}")
print(f"persentase     : {(with_cjk / total * 100) if total else 0:.2f}%")
if examples:
    print("\ncontoh:")
    for doc, chunk, text in examples:
        print(f"  [{doc}] {chunk}: {text}")
