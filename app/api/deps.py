"""Composition root: every long-lived object is built once per process."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Optional

from fastapi import Request

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.unanswered import UnansweredStore, store_path as unanswered_store_path
from app.jev.router import JevRouter
from app.jev.tools import JevClient
from app.qdrant import client as qdrant_client
from app.rag.embedder import EmbedderService
from app.rag.embedding_migration import EmbeddingMigrator
from app.rag.generator import Generator, build_llm_client
from app.rag.pipeline import RagPipeline
from app.rag.reranker import RerankerService
from app.rag.retriever import Retriever
from app.rag.sparse import SparseIndex, migrate_legacy_scopes, qdrant_rows_loader
from app.tables.store import TableStore
from app.workers.indexing import IndexingPipeline, IndexingWorker, JobStore

logger = get_logger(__name__)


@dataclass
class Services:
    settings: Settings
    embedder: EmbedderService
    migrator: EmbeddingMigrator
    reranker: RerankerService
    sparse: SparseIndex
    retriever: Retriever
    generator: Generator
    jev: JevRouter
    jobs: JobStore
    tables: TableStore
    unanswered: UnansweredStore
    indexing: IndexingPipeline
    worker: IndexingWorker
    rag: RagPipeline

    async def startup(self) -> None:
        # Warm the vector store before the worker can touch it: concurrent first
        # imports of the qdrant client from a worker thread have raced before.
        try:
            await asyncio.to_thread(qdrant_client.get_client, self.settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("qdrant not reachable at startup: %s", exc)
        try:
            await asyncio.to_thread(self.migrator.activate)
        except Exception as exc:  # noqa: BLE001 - layanan tetap jalan dengan koleksi saat ini
            logger.warning("aktivasi embedding gagal: %s", exc)
        recovered = self.jobs.recover_interrupted()
        if recovered:
            logger.warning("%d job yang terputus oleh restart ditutup (lihat statusnya di daftar dokumen)", recovered)
        await self.worker.start()
        if self.settings.sparse_rebuild_on_startup:
            # Indeks BM25 versi lama dibangun ulang dari Qdrant di latar belakang: pencarian tetap
            # berjalan dengan indeks lama sampai versi baru siap, lalu ditukar secara atomik.
            threading.Thread(
                target=self._rebuild_sparse, name="sparse-rebuild", daemon=True
            ).start()

    def _rebuild_sparse(self) -> None:
        try:
            done = migrate_legacy_scopes(self.sparse, qdrant_rows_loader(self.settings))
            if done:
                logger.info("pembangunan ulang BM25 selesai: %s", done)
        except Exception as exc:  # noqa: BLE001 - pemeliharaan, bukan jalur wajib
            logger.warning("pembangunan ulang BM25 gagal: %s", exc)

    async def shutdown(self) -> None:
        await self.worker.stop()
        self.embedder.unload()
        self.reranker.unload()
        try:
            self.tables.close()
        except Exception as exc:  # noqa: BLE001 - penutupan store tidak boleh menggagalkan shutdown
            logger.warning("tutup table store gagal: %s", exc)
        try:
            self.unanswered.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("tutup penyimpanan pertanyaan tak terjawab gagal: %s", exc)


_services: Optional[Services] = None
_lock = threading.Lock()


def build_services(settings: Optional[Settings] = None) -> Services:
    settings = settings or get_settings()
    embedder = EmbedderService(settings)
    migrator = EmbeddingMigrator(settings, embedder)
    reranker = RerankerService(settings)
    sparse = SparseIndex(settings)
    retriever = Retriever(settings, embedder, reranker, sparse)
    generator = Generator(settings, build_llm_client(settings))
    jev = JevRouter(settings, JevClient(settings))
    jobs = JobStore(settings.job_store_path)
    tables = TableStore(settings.table_store_path)
    unanswered = UnansweredStore(unanswered_store_path(settings))
    indexing = IndexingPipeline(settings, embedder, sparse, jobs, tables, generator=generator)
    worker = IndexingWorker(settings, indexing, jobs)
    rag = RagPipeline(settings, retriever, generator, jev, tables)
    return Services(
        settings=settings,
        embedder=embedder,
        migrator=migrator,
        reranker=reranker,
        sparse=sparse,
        retriever=retriever,
        generator=generator,
        jev=jev,
        jobs=jobs,
        tables=tables,
        unanswered=unanswered,
        indexing=indexing,
        worker=worker,
        rag=rag,
    )


def get_services() -> Services:
    global _services
    with _lock:
        if _services is None:
            _services = build_services()
        return _services


def set_services(services: Optional[Services]) -> None:
    """Test hook."""
    global _services
    with _lock:
        _services = services


def services_from_request(request: Request) -> Services:
    services = getattr(request.app.state, "services", None)
    return services or get_services()
