"""Periksa bentuk PointStruct yang tersimpan di storage.sqlite (read-only).

Fokus: bagaimana ``id`` di dalam record pickle, dan bagaimana ``vector`` disimpan.
"""

from __future__ import annotations

import base64
import pickle
import sqlite3

DB = "/data/qdrant/collection/knowledge_chunks/storage.sqlite"


def main() -> int:
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = connection.execute("SELECT id, point FROM points LIMIT 2").fetchall()
    connection.close()

    for raw_id, blob in rows:
        print("== kolom id ==")
        print("  tipe:", type(raw_id).__name__, "| nilai:", repr(raw_id)[:70])
        record = pickle.loads(blob)
        print("== record ==")
        print("  tipe:", type(record).__name__)
        print("  atribut:", [a for a in dir(record) if not a.startswith("_")][:15])
        print("  record.id:", repr(getattr(record, "id", None))[:90])
        vector = getattr(record, "vector", None)
        print("  vector tipe:", type(vector).__name__, "| panjang:", len(vector) if hasattr(vector, "__len__") else "?")
        if isinstance(vector, dict):
            for key, value in list(vector.items())[:3]:
                print(f"    vektor '{key}': {type(value).__name__} panjang={len(value) if hasattr(value, '__len__') else '?'}")
        payload = getattr(record, "payload", None) or {}
        print("  payload kunci:", sorted(payload.keys())[:10])
        print()

    # Berapa banyak id yang butuh padding, dan apakah hasilnya UUID valid.
    import uuid

    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    all_ids = [row[0] for row in connection.execute("SELECT id FROM points").fetchall()]
    connection.close()
    ok, bad = 0, []
    for raw in all_ids:
        text = raw if isinstance(raw, str) else bytes(raw).decode("latin1", "replace")
        decoded = None
        for candidate in (text, text + "=" * (-len(text) % 4)):
            try:
                decoded = pickle.loads(base64.b64decode(candidate))
                break
            except Exception:  # noqa: BLE001
                continue
        try:
            uuid.UUID(str(decoded))
            ok += 1
        except Exception:  # noqa: BLE001
            bad.append(decoded)
    print("id valid UUID:", ok, "| tidak valid:", len(bad))
    for item in bad[:5]:
        print("   ", repr(item)[:100])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
