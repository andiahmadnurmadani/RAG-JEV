"""Ganti model embedding tanpa mengunggah ulang knowledge.

Setiap model punya koleksi Qdrant sendiri (``knowledge_chunks`` untuk ``hash``,
``knowledge_chunks__<model>`` untuk model semantik), karena vektor dari model berbeda tidak
bisa dibandingkan dan dimensinya pun bisa berbeda. Saat model aktif berganti, seluruh
potongan dari koleksi yang aktif SEBELUMNYA di-embed ulang ke koleksi baru di latar belakang,
dari isi teks yang tersimpan di payload. Koleksi lama tidak dihapus (bisa dipakai kembali).

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
        """Pakai koleksi milik model aktif; migrasikan bila koleksinya belum berisi."""
        from app.qdrant.client import get_client

        settings = self._settings
        target = collection_for(self._base, settings.embedding_provider, settings.embedding_fastembed_model)
        with self._lock:
            previous = str(self._state.get("active") or self._base)
            pending = self._state.get("state") == "running" and self._state.get("target") == target
            settings.qdrant_collection = target
            self._token += 1
            token = self._token
        source = str(self._state.get("source") or previous) if pending else previous
        if source == target:
            self._save(active=target)
            return target
        try:
            client = get_client(settings)
            has_source = client.collection_exists(source) and client.count(collection_name=source, exact=True).count > 0
            target_count = client.count(collection_name=target, exact=True).count if client.collection_exists(target) else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("cek koleksi untuk migrasi embedding gagal: %s", exc)
            self._save(active=target)
            return target
        if not has_source or (target_count and not pending):
            self._save(active=target, state="done" if target_count else "idle", source=source, target=target)
            return target
        self._save(active=target, state="running", source=source, target=target, done=0, total=0,
                   started_at=_now(), finished_at=None, error=None)
        if start:
            self._thread = threading.Thread(target=self._run, args=(source, target, token),
                                            name="embedding-migration", daemon=True)
            self._thread.start()
        return target

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
                    collection_name=source, limit=BATCH, offset=offset, with_payload=True, with_vectors=False
                )
                payloads = [dict(record.payload or {}) for record in records]
                payloads = [payload for payload in payloads if payload.get("organization_id") and payload.get("chunk_id")]
                if payloads:
                    vectors = self._embedder.encode([_contextual_text_from_payload(p) for p in payloads])
                    repository.upsert_chunks(
                        settings,
                        dim=len(vectors[0]),
                        points=[{"chunk_id": p["chunk_id"], "vector": v, "payload": p} for p, v in zip(payloads, vectors)],
                    )
                done += len(records)
                self._save(done=done)
                if offset is None or not records:
                    break
            self._save(state="done", finished_at=_now())
            logger.info("migrasi embedding %s -> %s selesai: %d potongan dalam %.0f dtk",
                        source, target, done, time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001
            logger.exception("migrasi embedding gagal")
            self._save(state="failed", error=str(exc)[:300], finished_at=_now())
