"""Cari tahu bagaimana ID titik tersimpan di storage.sqlite Qdrant lokal (read-only)."""

from __future__ import annotations

import base64
import pickle
import sqlite3
import uuid

DB = "/data/qdrant/collection/knowledge_chunks/storage.sqlite"


def main() -> int:
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = connection.execute("SELECT id FROM points LIMIT 3").fetchall()
    connection.close()
    for (raw,) in rows:
        print("raw tipe:", type(raw).__name__, "| nilai:", repr(raw)[:90])
        candidates = {}
        if isinstance(raw, str):
            try:
                candidates["b64->pickle"] = pickle.loads(base64.b64decode(raw))
            except Exception as exc:  # noqa: BLE001
                candidates["b64->pickle"] = f"gagal: {exc}"
            try:
                candidates["latin1->pickle"] = pickle.loads(raw.encode("latin1"))
            except Exception as exc:  # noqa: BLE001
                candidates["latin1->pickle"] = f"gagal: {exc}"
            try:
                candidates["uuid str"] = str(uuid.UUID(raw))
            except Exception as exc:  # noqa: BLE001
                candidates["uuid str"] = f"gagal: {exc}"
        elif isinstance(raw, (bytes, bytearray)):
            try:
                candidates["pickle(bytes)"] = pickle.loads(bytes(raw))
            except Exception as exc:  # noqa: BLE001
                candidates["pickle(bytes)"] = f"gagal: {exc}"
            try:
                candidates["b64(bytes)->pickle"] = pickle.loads(base64.b64decode(bytes(raw)))
            except Exception as exc:  # noqa: BLE001
                candidates["b64(bytes)->pickle"] = f"gagal: {exc}"
        for name, value in candidates.items():
            print(f"   {name}: {value!r}"[:160])
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
