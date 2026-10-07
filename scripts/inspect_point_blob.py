"""Periksa bentuk kolom `point` pada storage.sqlite Qdrant embedded."""

from __future__ import annotations

import pickle
import sqlite3
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/st.sqlite"
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
row = db.execute("select point from points limit 1").fetchone()
blob = row[0]
print("tipe kolom :", type(blob).__name__)
if isinstance(blob, (bytes, bytearray)):
    print("panjang    :", len(blob))
    print("10 byte awal:", blob[:10])
    print("bisa utf-8? :", end=" ")
    try:
        head = blob[:400].decode("utf-8", errors="replace")
        print(head[:300])
    except Exception as exc:  # noqa: BLE001
        print("gagal:", exc)
try:
    obj = pickle.loads(blob if isinstance(blob, (bytes, bytearray)) else bytes(blob))
    print("\npickle OK  :", type(obj).__name__)
    print("atribut    :", [a for a in dir(obj) if not a.startswith("_")][:15])
    payload = getattr(obj, "payload", None)
    if isinstance(payload, dict):
        print("payload key:", list(payload)[:15])
        print("content    :", str(payload.get("content"))[:120])
except Exception as exc:  # noqa: BLE001
    print("\npickle GAGAL:", type(exc).__name__, exc)
