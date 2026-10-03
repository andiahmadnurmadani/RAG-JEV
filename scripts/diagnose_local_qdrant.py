"""Diagnosa Qdrant lokal: apakah data rusak, dan seberapa jauh.

Dijalankan DI DALAM container. Tidak mencetak rahasia.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/srv")

from app.core.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    print("collection   :", settings.qdrant_collection)
    print("qdrant_url   :", repr(settings.qdrant_url), "(kosong = lokal/embedded)")
    print("local path   :", settings.qdrant_local_path)

    base = Path(settings.qdrant_local_path)
    print("\n== berkas di penyimpanan lokal ==")
    files = sorted(p for p in base.rglob("*") if p.is_file())
    for path in files[:40]:
        print("  ", path.relative_to(base), path.stat().st_size, "byte")

    print("\n== bentuk array .npy ==")
    try:
        import numpy as np

        for path in sorted(base.rglob("*.npy")):
            try:
                array = np.load(path)
                print("  ", path.name, "->", array.shape, array.dtype)
            except Exception as exc:  # noqa: BLE001
                print("  ", path.name, "-> GAGAL dibaca:", type(exc).__name__, exc)
    except ImportError:
        print("   numpy tidak ada")

    print("\n== uji operasi klien ==")
    from app.qdrant.client import get_client

    client = get_client(settings)
    name = settings.qdrant_collection
    print("  collection_exists:", client.collection_exists(name))
    for label, call in (
        ("get_collection", lambda: client.get_collection(name)),
        ("count(exact)", lambda: client.count(name, exact=True)),
        ("scroll(3)", lambda: client.scroll(name, limit=3, with_payload=False, with_vectors=False)),
    ):
        try:
            result = call()
            if label == "scroll(3)":
                print(f"  {label}: ok, {len(result[0])} titik")
            elif label == "count(exact)":
                print(f"  {label}: ok, {result.count}")
            else:
                print(f"  {label}: ok, points={getattr(result, 'points_count', '?')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {label}: GAGAL {type(exc).__name__}: {exc}")

    print("\n== jumlah titik per organisasi (scroll manual) ==")
    try:
        counts: dict[str, int] = {}
        offset = None
        total = 0
        while True:
            points, offset = client.scroll(
                name, limit=256, offset=offset, with_payload=True, with_vectors=False
            )
            for point in points:
                org = (point.payload or {}).get("organization_id", "?")
                counts[org] = counts.get(org, 0) + 1
                total += 1
            if offset is None or not points:
                break
        print("  total terbaca:", total)
        for org, count in sorted(counts.items(), key=lambda item: -item[1])[:10]:
            print(f"    {org}: {count}")
    except Exception as exc:  # noqa: BLE001
        print("  scroll GAGAL:", type(exc).__name__, exc)

    print("\n== kunci API aktif (permission saja, tanpa nilai) ==")
    for path in ("/data/api_keys.json", "/data/bootstrap_admin_key.json"):
        if not Path(path).exists():
            continue
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict) and data.get("key"):
            print(f"  {path}: satu kunci (bootstrap)")
            continue
        records = data.get("keys") if isinstance(data, dict) else data
        items = records.values() if isinstance(records, dict) else (records or [])
        for record in items:
            if record.get("revoked_at") or record.get("revoked"):
                continue
            print(
                "  {} | org={} | label={} | izin={}".format(
                    path.split("/")[-1],
                    record.get("organization_id"),
                    record.get("label") or record.get("name") or "-",
                    record.get("permissions"),
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
