"""Inventaris cepat untuk pemulihan: berapa data yang ada, dan apa saja sumbernya.

Dijalankan DI DALAM container. READ-ONLY. Tidak mencetak rahasia.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def main() -> int:
    print("== titik di storage.sqlite ==")
    db = Path("/data/qdrant/collection/knowledge_chunks/storage.sqlite")
    if db.exists():
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        print("  tabel:", tables)
        for table in tables:
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {count} baris")
        connection.close()
    else:
        print("  tidak ada")

    print("\n== indeks sparse (cadangan teks per KB) ==")
    files = sorted(Path("/data/sparse").rglob("*.json"))
    total = 0
    per_kb: dict[str, int] = {}
    for path in files:
        try:
            data = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            continue
        entries = data.get("entries") if isinstance(data, dict) else data
        count = len(entries or [])
        total += count
        if count:
            per_kb[path.stem] = count
    print("  berkas:", len(files), "| total entri:", total)
    for name, count in sorted(per_kb.items(), key=lambda item: -item[1])[:15]:
        print(f"    {name}: {count}")

    print("\n== jobs.json (daftar dokumen) ==")
    path = Path("/data/jobs.json")
    if path.exists():
        data = json.loads(path.read_text())
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        items = list(jobs.values() if isinstance(jobs, dict) else jobs)
        alive = [j for j in items if j.get("status") not in ("deleted", "failed")]
        print("  total:", len(items), "| aktif:", len(alive))
        sources = {}
        for job in alive:
            source = job.get("source_url") or "-"
            sources[source] = sources.get(source, 0) + 1
        print("  sumber (source_url):")
        for source, count in sorted(sources.items(), key=lambda item: -item[1])[:10]:
            print(f"    {source}: {count}")
    else:
        print("  tidak ada")

    print("\n== berkas asli di /data/storage ==")
    storage = Path("/data/storage")
    files = [p for p in storage.rglob("*") if p.is_file()] if storage.exists() else []
    print("  jumlah:", len(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
