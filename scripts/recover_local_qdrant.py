"""Pemulihan Qdrant lokal: bangun ulang koleksi dari titik yang masih ada di storage.sqlite.

Latar: klien Qdrant *embedded* tidak thread-safe. Dua penulis bersamaan (pengindeksan +
ringkasan) merusak struktur internalnya, sehingga pembacaan gagal permanen
(``operands could not be broadcast together with shapes (5432,) (5431,)`` /
``index 5431 is out of bounds for axis 0 with size 5431``). Titik-titiknya sendiri masih utuh
di ``storage.sqlite`` (vektor + payload) - yang rusak hanya struktur indeksnya.

Karena folder storage terkunci oleh layanan yang berjalan, skrip ini bekerja pada SALINAN:

    python3 recover_local_qdrant.py --source /data/qdrant --target /tmp/qdrant_fix

Folder asli tidak disentuh; penggantinya diverifikasi dulu (jumlah titik + satu query nyata).
"""

from __future__ import annotations

import argparse
import pickle
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "/srv")


def read_points(db_path: Path):
    """Baca (id, payload, vector) langsung dari storage.sqlite tanpa klien Qdrant.

    ID diambil dari ``record.id`` (UUID yang sudah didekode pickle), BUKAN dari kolom ``id``
    yang masih berbentuk ``base64(pickle(...))`` - memakai kolom itu membuat upsert menolak
    dengan "not a valid UUID". Vektor dan payload juga sudah siap di dalam record.
    """
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT point FROM points").fetchall()
    finally:
        connection.close()
    points = []
    for (blob,) in rows:
        record = pickle.loads(blob)
        points.append((record.id, record))
    return points


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="/data/qdrant", help="folder storage yang rusak")
    parser.add_argument("--target", default="/tmp/qdrant_fix", help="folder salinan untuk perbaikan")
    parser.add_argument("--collection", default="knowledge_chunks")
    parser.add_argument("--dim", type=int, default=384)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = Path(args.source)
    db_path = source / "collection" / args.collection / "storage.sqlite"
    if not db_path.exists():
        print("storage.sqlite tidak ditemukan:", db_path)
        return 1
    raw = read_points(db_path)
    print("titik terbaca dari sumber:", len(raw))
    sample = raw[0][1] if raw else None
    print("contoh payload:", sorted((getattr(sample, "payload", None) or {}).keys())[:8])
    vectors = sum(1 for _, record in raw if getattr(record, "vector", None) is not None)
    print("punya vektor:", vectors)
    if args.dry_run:
        print("(dry-run: tidak menulis)")
        return 0

    target = Path(args.target)
    if target.exists():
        shutil.rmtree(target)
    print("menyalin", source, "->", target)
    shutil.copytree(source, target)

    from qdrant_client import QdrantClient, models

    client = QdrantClient(path=str(target))
    if client.collection_exists(args.collection):
        client.delete_collection(args.collection)
    client.create_collection(
        collection_name=args.collection,
        vectors_config=models.VectorParams(size=args.dim, distance=models.Distance.COSINE),
    )

    batch, written = [], 0
    for point_id, record in raw:
        vector = getattr(record, "vector", None)
        if vector is None:
            continue
        batch.append(models.PointStruct(
            id=point_id, vector=vector, payload=getattr(record, "payload", None) or {}))
        if len(batch) >= 256:
            client.upsert(args.collection, points=batch)
            written += len(batch)
            batch = []
    if batch:
        client.upsert(args.collection, points=batch)
        written += len(batch)

    count = client.count(args.collection, exact=True).count
    print("titik ditulis:", written, "| dibaca ulang:", count)
    if count != written:
        print("GAGAL: jumlah tidak cocok")
        return 1

    # Bukti akhir: satu pencarian benar-benar berjalan (inilah yang tadinya gagal 500).
    try:
        hits = client.query_points(
            collection_name=args.collection,
            query=[0.0] * args.dim,
            limit=2,
            with_payload=False,
        ).points
        print("uji query: ok,", len(hits), "hasil")
    except Exception as exc:  # noqa: BLE001
        print("uji query GAGAL:", type(exc).__name__, exc)
        return 1
    client.close()
    print("OK ->", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
