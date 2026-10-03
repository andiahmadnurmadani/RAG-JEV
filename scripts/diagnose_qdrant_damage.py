"""Diagnosa kerusakan Qdrant lokal: apa yang rusak, dan apa yang masih bisa diselamatkan.

Dijalankan DI DALAM container. Tidak mencetak rahasia. READ-ONLY: tidak mengubah apa pun.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "/srv")


def main() -> int:
    from app.core.config import get_settings

    settings = get_settings()
    root = Path(settings.qdrant_local_path)
    print("collection:", settings.qdrant_collection)
    print("root      :", root)

    print("\n== meta.json ==")
    meta_path = root / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        print("  kunci:", list(meta.keys()))
        collections = meta.get("collections") or {}
        for name, info in collections.items():
            print("  ", name, "->", json.dumps(info)[:200])

    print("\n== schema storage.sqlite ==")
    db = root / "collection" / settings.qdrant_collection / "storage.sqlite"
    if not db.exists():
        # coba cari
        found = list(root.rglob("storage.sqlite"))
        print("  tidak di jalur dugaan; ditemukan:", [str(p) for p in found])
        if found:
            db = found[0]
    if not db.exists():
        print("  TIDAK ADA storage.sqlite")
        return 1
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    print("  tabel:", tables)
    for table in tables:
        try:
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            print(f"    {table}: {count} baris | kolom: {columns}")
        except Exception as exc:  # noqa: BLE001
            print(f"    {table}: GAGAL {exc}")

    print("\n== apakah titik masih bisa dibaca langsung dari sqlite? ==")
    for table in tables:
        if "point" not in table.lower():
            continue
        try:
            rows = connection.execute(f"SELECT * FROM {table} LIMIT 2").fetchall()
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            print(f"  {table} contoh baris (kolom: {columns}):")
            for row in rows:
                preview = []
                for value in row:
                    if isinstance(value, (bytes, bytearray)):
                        preview.append(f"<{len(value)} byte>")
                    else:
                        preview.append(str(value)[:60])
                print("   ", preview)
        except Exception as exc:  # noqa: BLE001
            print(f"  {table}: GAGAL {exc}")

    print("\n== dokumen yang tercatat di jobs.json (untuk reindex) ==")
    jobs_path = Path("/data/jobs.json")
    if jobs_path.exists():
        data = json.loads(jobs_path.read_text())
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        items = list(jobs.values() if isinstance(jobs, dict) else jobs)
        alive = [j for j in items if j.get("status") not in ("deleted", "failed")]
        print("  total job:", len(items), "| aktif:", len(alive))
        print("  kolom job:", sorted(items[0].keys()) if items else "-")
        for job in alive[:6]:
            print("   -", job.get("document_id"), "| kb=", job.get("knowledge_base_id"),
                  "| sumber=", job.get("source_url") or job.get("storage_path") or "-",
                  "| chunk=", job.get("chunks"))
    else:
        print("  jobs.json tidak ada")

    print("\n== berkas asli untuk reindex (storage dir) ==")
    storage = Path("/data/storage")
    if storage.exists():
        files = [p for p in storage.rglob("*") if p.is_file()]
        print("  jumlah berkas:", len(files))
        for path in files[:8]:
            print("   -", path, path.stat().st_size, "byte")
    else:
        print("  tidak ada /data/storage")

    print("\n== indeks sparse (cadangan teks) ==")
    sparse = Path("/data/sparse")
    if sparse.exists():
        files = sorted(p for p in sparse.rglob("*.json"))
        print("  berkas:", len(files))
        for path in files[:5]:
            try:
                payload = json.loads(path.read_text())
                entries = payload.get("entries") if isinstance(payload, dict) else payload
                print("   -", path.name, "->", len(entries or []), "entri")
            except Exception as exc:  # noqa: BLE001
                print("   -", path.name, "-> GAGAL", exc)
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
