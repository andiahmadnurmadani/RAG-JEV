"""Klien Qdrant lokal harus aman dari penulisan bersamaan.

Bug nyata yang melahirkan uji ini: klien Qdrant *embedded* (``path=``) tidak thread-safe.
Setelah ringkasan mulai berjalan di worker terpisah, dua penulis menyentuh koleksi bersamaan
dan struktur internalnya rusak permanen - seluruh layanan jadi 500:

    operands could not be broadcast together with shapes (5432,) (5431,)
    Qdrant search failed: index 5431 is out of bounds for axis 0 with size 5431

Perbaikannya: di mode lokal, setiap panggilan klien diserialkan lewat satu kunci re-entrant
(``SerialisedClient``). Uji ini menulis dari banyak thread lalu memastikan pembacaan tetap
konsisten - tanpa serialisasi, ini gagal dengan galat bentuk array di atas.
"""

from __future__ import annotations

import threading

from app.core.config import get_settings
from app.qdrant.client import SerialisedClient, build_client, get_client


def test_local_client_is_wrapped_so_calls_are_serialised(settings):
    """Mode lokal (QDRANT_URL kosong) harus memakai pembungkus yang menyerialkan panggilan.

    Diperiksa lewat ``get_client`` (singleton), bukan ``build_client``: membangun klien kedua
    pada folder yang sama akan ditolak Qdrant ("already accessed") - dan itu memang benar.
    """
    client = get_client(settings)
    assert isinstance(client, SerialisedClient), (
        "klien Qdrant lokal tidak diserialkan: penulisan bersamaan akan merusak koleksi"
    )


def test_serialised_client_delegates_attributes_and_calls():
    """Pembungkus harus meneruskan atribut non-fungsi apa adanya dan memanggil yang fungsi."""

    class Fake:
        def __init__(self) -> None:
            self.marker = "nilai"

        def add(self, a: int, b: int) -> int:
            return a + b

    wrapper = SerialisedClient(Fake())
    assert wrapper.marker == "nilai"
    assert wrapper.add(2, 3) == 5


def test_concurrent_writes_do_not_corrupt_the_collection(settings):
    """Banyak thread menulis lalu membaca: tanpa serialisasi, koleksi rusak permanen."""
    from app.qdrant import collections, repository

    settings.embedding_dim = 384
    client = get_client(settings)
    collections.ensure_collection(settings, 384)

    # Titik-titik yang akan ditulis dari banyak thread sekaligus. Bentuknya dict, seperti yang
    # dipakai pipeline pengindeksan sungguhan.
    def make_points(start: int, count: int):
        return [
            {
                "vector": [float((start + index) % 7) / 7.0] * 384,
                "payload": {
                    "organization_id": "org_a",
                    "knowledge_base_id": "kb_race",
                    "document_id": "doc_race",
                    "chunk_id": f"chunk_{start + index:04d}",
                    "content": f"isi {start + index}",
                },
            }
            for index in range(count)
        ]

    errors: list[str] = []

    def writer(worker: int) -> None:
        try:
            repository.upsert_chunks(
                settings, dim=384, points=make_points(worker * 50, 50)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    def reader() -> None:
        try:
            for _ in range(20):
                client.count(settings.qdrant_collection, exact=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"baca: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(6)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"penulisan/pembacaan bersamaan gagal: {errors[:3]}"

    # Pembacaan setelahnya harus konsisten (inilah yang dulu melempar galat bentuk array).
    info = collections.collection_info(settings)
    assert info["exists"] is True
    assert info["points"] == 300, info
    found = repository.list_document_chunks(
        settings, organization_id="org_a", document_id="doc_race", knowledge_base_id="kb_race"
    )
    assert len(found) == 300, len(found)
