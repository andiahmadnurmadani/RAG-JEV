"""Baca isi koleksi Qdrant tertanam langsung dari SQLite (hanya baca, tanpa klien Qdrant).

storage.sqlite menyimpan tiap titik sebagai pickle PointStruct; skrip ini membongkarnya
untuk membuktikan apa saja yang benar-benar disimpan per chunk.
"""
import pickle
import sqlite3

DB = "data/live/qdrant/collection/live_chunks/storage.sqlite"
con = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
cur = con.cursor()
total = cur.execute("select count(*) from points").fetchone()[0]
print("jumlah titik:", total)

rows = cur.execute("select id, point from points limit 3").fetchall()
con.close()

for key, blob in rows:
    obj = pickle.loads(blob)
    data = obj.__dict__ if hasattr(obj, "__dict__") else obj
    payload = dict(data.get("payload") or {})
    vector = data.get("vector")
    print("\n=== titik", str(key)[:32], "===")
    print("  id di dalam titik:", data.get("id"))
    print("  kunci payload:", sorted(payload))
    for name in sorted(payload):
        value = payload[name]
        if isinstance(value, str) and len(value) > 220:
            value = value[:220] + " …"
        print(f"    {name} = {value!r}")
    if hasattr(vector, "__len__"):
        print("  vektor:", type(vector).__name__, "panjang", len(vector), "| 4 nilai pertama:",
              [round(float(x), 5) for x in list(vector)[:4]])
    else:
        print("  vektor:", type(vector).__name__)

# distribusi dokumen di koleksi
con = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
cur = con.cursor()
counts = {}
for (blob,) in cur.execute("select point from points"):
    payload = pickle.loads(blob).__dict__.get("payload") or {}
    k = (payload.get("organization_id"), payload.get("knowledge_base_id"), payload.get("document_id"))
    counts[k] = counts.get(k, 0) + 1
con.close()
print("\n=== titik per (tenant, knowledge base, dokumen) ===")
for k, v in sorted(counts.items()):
    print("  ", k, "->", v, "chunk")
