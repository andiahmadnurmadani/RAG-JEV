"""Ganti model embedding tanpa mengunggah ulang knowledge.

Setiap model punya koleksi Qdrant sendiri (``knowledge_chunks`` untuk ``hash``,
``knowledge_chunks__<model>`` untuk model semantik), karena vektor dari model berbeda tidak
bisa dibandingkan dan dimensinya pun bisa berbeda. Saat model aktif berganti, seluruh
potongan dari koleksi yang aktif SEBELUMNYA di-embed ulang ke koleksi baru di latar belakang,
dari isi teks yang tersimpan di payload. Koleksi sumber tidak dihapus; bila model lama dipilih
lagi, koleksinya dibangun ulang dari koleksi aktif (isinya sudah basi sejak ditinggal).
Selama migrasi, penghapusan dokumen/KB juga diterapkan ke koleksi sumber
(``settings.qdrant_migration_source``) supaya salinan tidak menghidupkannya lagi.

Status disimpan di ``embedding_state.json`` (volume data yang sama), sehingga migrasi yang
terputus oleh restart dilanjutkan otomatis.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.jsonfile import read_json, write_json_atomic
from app.core.logging import get_logger

logger = get_logger(__name__)

# Model yang didukung + label untuk UI. Model lain tetap bisa dipakai lewat env.
SUPPORTED_MODELS: Dict[str, str] = {
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2": "mpnet multibahasa (disarankan: paling akurat untuk bahasa Indonesia)",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": "MiniLM multibahasa (lebih cepat, sedikit kurang akurat)",
    "intfloat/multilingual-e5-large": "multilingual-e5-large (besar, lambat)",
}
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
BATCH = 16
# Halaman scroll Qdrant. Qdrant tertanam memindai seluruh koleksi di SETIAP halaman, jadi halaman
# kecil membuat migrasi kuadratik terhadap jumlah potongan.
PAGE = 512
INCOMPLETE = ("running", "failed", "interrupted")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def model_slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(model or "").split("/")[-1].lower()).strip("-")[:48] or "model"


def collection_for(base: str, provider: str, model: str) -> str:
    if provider == "hash":
        return base
    return f"{base}__{model_slug(model)}"


class EmbeddingMigrator:
    def __init__(self, settings: Any, embedder: Any) -> None:
        self._settings = settings
        self._embedder = embedder
        self._base = str(settings.qdrant_collection)
        self._path = Path(settings.table_store_path).with_name("embedding_state.json")
        self._lock = threading.RLock()
        self._token = 0
        self._thread: Optional[threading.Thread] = None
        stored = read_json(self._path)
        self._state: Dict[str, Any] = stored if isinstance(stored, dict) else {}

    @property
    def base_collection(self) -> str:
        return self._base

    def status(self) -> Dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        state["provider"] = self._settings.embedding_provider
        state["model"] = self._settings.embedding_fastembed_model if self._settings.embedding_provider == "fastembed" else "hash"
        state["collection"] = self._settings.qdrant_collection
        return state

    def _save(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)
            try:
                write_json_atomic(self._path, self._state)
            except OSError as exc:  # pragma: no cover - disk penuh/izin
                logger.warning("status migrasi embedding tidak tersimpan: %s", exc)

    def activate(self, *, start: bool = True) -> str:
        """Pakai koleksi milik model aktif; migrasikan bila koleksinya belum lengkap.

        * Sumber migrasi selalu koleksi LENGKAP terakhir. Migrasi yang belum selesai (gagal,
          terputus, atau ditinggal karena model diganti lagi) tidak pernah dijadikan sumber.
        * Migrasi yang belum selesai ke target yang sama dilanjutkan, bukan dianggap selesai.
        * Koleksi target sisa pemakaian lama dibangun ulang: isinya basi (dokumen yang dihapus
          atau diubah sejak itu akan muncul lagi bila dipakai apa adanya).
        """
        from app.qdrant.client import get_client

        settings = self._settings
        target = collection_for(self._base, settings.embedding_provider, settings.embedding_fastembed_model)
        with self._lock:
            state = dict(self._state)
            incomplete = state.get("state") in INCOMPLETE
            previous = str(state.get("active") or self._base)
            source = str(state.get("source") or previous) if incomplete else previous
            resume = incomplete and state.get("target") == target
            settings.qdrant_collection = target
            self._token += 1
            token = self._token
        if source == target:
            # Kembali ke koleksi lengkap (sumber): migrasi yang belum selesai ditinggal, tetapi
            # dicatat supaya bila model itu dipilih lagi, migrasinya dilanjutkan dari sumber ini.
            settings.qdrant_migration_source = ""
            self._save(active=target, state="interrupted" if incomplete else (state.get("state") or "idle"))
            return target
        try:
            client = get_client(settings)
            has_source = client.collection_exists(source) and client.count(collection_name=source, exact=True).count > 0
            target_exists = client.collection_exists(target)
            if has_source and target_exists and not resume:
                client.delete_collection(target)
                logger.info("koleksi %s sisa pemakaian lama dibangun ulang dari %s", target, source)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cek koleksi untuk migrasi embedding gagal: %s", exc)
            self._save(active=target)
            return target
        if not has_source:
            settings.qdrant_migration_source = ""
            self._save(active=target, state="idle", source=source, target=target)
            return target
        settings.qdrant_migration_source = source
        changes: Dict[str, Any] = dict(active=target, state="running", source=source, target=target, error=None,
                                       finished_at=None)
        if not resume:
            changes.update(done=0, total=0, started_at=_now())
        self._save(**changes)
        if start:
            self._thread = threading.Thread(target=self._run, args=(source, target, token),
                                            name="embedding-migration", daemon=True)
            self._thread.start()
        return target

    @staticmethod
    def _missing_in_target(client: Any, target: str, payloads: list) -> list:
        from app.qdrant.repository import point_id

        if not payloads:
            return payloads
        try:
            if not client.collection_exists(target):
                return payloads
            ids = [point_id(p.get("chunk_id", ""), organization_id=p.get("organization_id", ""),
                            knowledge_base_id=p.get("knowledge_base_id", ""), document_id=p.get("document_id", ""))
                   for p in payloads]
            present = {str(record.id) for record in client.retrieve(collection_name=target, ids=ids,
                                                                     with_payload=False, with_vectors=False)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("cek potongan yang sudah ada di %s gagal: %s", target, exc)
            return payloads
        return [p for p, pid in zip(payloads, ids) if pid not in present]

    def _run(self, source: str, target: str, token: int) -> None:
        from app.qdrant import repository
        from app.qdrant.client import get_client
        from app.workers.indexing import _contextual_text_from_payload

        settings = self._settings
        started = time.perf_counter()
        try:
            client = get_client(settings)
            total = client.count(collection_name=source, exact=True).count
            self._save(total=total)
            done = 0
            offset = None
            while True:
                if token != self._token:
                    logger.info("migrasi embedding %s -> %s dihentikan (model berganti lagi)", source, target)
                    return
                records, offset = client.scroll(
                    collection_name=source, limit=PAGE, offset=offset, with_payload=True, with_vectors=False
                )
                payloads = [dict(record.payload or {}) for record in records]
                payloads = [payload for payload in payloads if payload.get("organization_id") and payload.get("chunk_id")]
                # Potongan yang SUDAH ada di target ditulis oleh pengindeksan baru selama migrasi
                # (atau oleh percobaan sebelumnya yang terputus): jangan ditimpa isi lama.
                payloads = self._missing_in_target(client, target, payloads)
                for begin in range(0, len(payloads), BATCH):
                    if token != self._token:
                        logger.info("migrasi embedding %s -> %s dihentikan (model berganti lagi)", source, target)
                        return
                    batch = payloads[begin : begin + BATCH]
                    vectors = self._embedder.encode([_contextual_text_from_payload(p) for p in batch])
                    repository.upsert_chunks(
                        settings,
                        dim=len(vectors[0]),
                        points=[{"chunk_id": p["chunk_id"], "vector": v, "payload": p} for p, v in zip(batch, vectors)],
                    )
                done += len(records)
                self._save(done=done)
                if offset is None or not records:
                    break
            with self._lock:
                if token == self._token:
                    settings.qdrant_migration_source = ""
            self._save(state="done", finished_at=_now())
            logger.info("migrasi embedding %s -> %s selesai: %d potongan dalam %.0f dtk",
                        source, target, done, time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001
            logger.exception("migrasi embedding gagal")
            self._save(state="failed", error=str(exc)[:300], finished_at=_now())
