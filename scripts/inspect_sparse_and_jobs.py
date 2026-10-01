"""Isi indeks sparse (BM25) dan job store (sekaligus daftar dokumen).

Jalankan dari akar repo:
    PYTHONPATH=. .venv/Scripts/python.exe scripts/inspect_sparse_and_jobs.py
"""
import json
from pathlib import Path

LIVE = Path("data/live")

print("=== indeks sparse: data/live/sparse/ ===")
for path in sorted((LIVE / "sparse").glob("*.json")):
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("entries", [])
    tokens = sum(len(e.get("tokens", [])) for e in entries)
    print(f"  {path.name}: {len(entries)} entri, {tokens} token "
          f"(org={raw.get('organization_id')}, kb={raw.get('knowledge_base_id')})")
    if entries:
        sample = entries[0]
        print(f"    contoh: chunk_id={sample.get('chunk_id')} document_id={sample.get('document_id')} "
              f"tokens[:8]={sample.get('tokens', [])[:8]}")
        print(f"    kunci per entri: {sorted(sample)}")

print("\n=== job store: data/live/jobs.json ===")
jobs = json.loads((LIVE / "jobs.json").read_text(encoding="utf-8"))
records = jobs.get("jobs", jobs if isinstance(jobs, list) else [])
print("jumlah record:", len(records))
if records:
    print("kunci record:", sorted(records[-1]))
    print("contoh record terakhir:")
    print(json.dumps({k: (str(v)[:80]) for k, v in records[-1].items()}, indent=2, ensure_ascii=False)[:900])

counts = {}
for rec in records:
    counts[rec.get("status")] = counts.get(rec.get("status"), 0) + 1
print("status record:", counts)

by_scope = {}
for rec in records:
    if rec.get("status") != "completed":
        continue
    key = (rec.get("organization_id"), rec.get("knowledge_base_id"))
    by_scope.setdefault(key, []).append((rec.get("document_id"), rec.get("chunks"), rec.get("tokens")))
print("\n=== dokumen selesai per (tenant, knowledge base) ===")
for key, docs in sorted(by_scope.items()):
    print(f"  {key}: {len(docs)} dokumen")
    for doc_id, chunks, tokens in docs:
        print(f"    {doc_id}: {chunks} chunk, {tokens} token")

print("\n=== penyimpanan berkas (STORAGE_DIR) ===")
stored = [p for p in (LIVE / "storage").rglob("*") if p.is_file()]
print("  isi:", [str(p) for p in stored[:10]] or "(kosong - byte/teks hasil parse tidak disimpan)")
