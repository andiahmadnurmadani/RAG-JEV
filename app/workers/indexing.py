"""Asynchronous indexing pipeline (PRD 10, 32, 33).

    parse -> clean -> chunk -> metadata -> embed -> Qdrant upsert -> (done)

The HTTP layer only enqueues (202 Accepted, status ``queued``/``processing``); a
pool of asyncio tasks pulls jobs and runs the CPU/IO-heavy work in a thread, so
large documents cannot time out the upload request (PRD 33).

Job state is kept in memory **and** on disk, so ``GET /knowledge/{document_id}``
still answers truthfully across a restart.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.metrics import incr as metric_incr
from app.core.metrics import observe as metric_observe
from app.core.security import sanitize_filename, validate_display_label, validate_upload
from app.parsing.parser import ParsedDocument, ParsedPage, parse_document
from app.qdrant import repository
from app.rag.chunker import chunk_document, estimate_tokens
from app.rag.embedder import EmbedderService
from app.rag.constants import SUMMARY_CHUNK_ID
from app.parsing.tables import extract_tables, supports_tables
from app.rag.sparse import SparseIndex
from app.tables.store import TableStore

logger = get_logger(__name__)

STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_DELETED = "deleted"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class JobRecord:
    job_id: str
    document_id: str
    organization_id: str
    knowledge_base_id: str
    document_name: str = ""
    status: str = STATUS_QUEUED
    stage: str = "queued"
    chunks: int = 0
    tokens: int = 0
    pages: int = 0
    tables: int = 0
    attempts: int = 0
    error: Optional[str] = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    duration_ms: Optional[float] = None
    source_url: str = ""
    # Ringkasan dokumen: dibuat sekali saat pengindeksan, disimpan bersama job, dan diindeks
    # sebagai potongan tersendiri supaya pertanyaan "ringkas dokumen ini" bisa dijawab dari
    # ringkasannya. Kosong = belum/tidak diringkas (mis. LLM dimatikan).
    summary: str = ""
    summary_tokens: int = 0
    summary_of: str = ""
    summary_error: str = ""

    def public(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload.pop("organization_id", None)  # never leak tenant ids in job output
        return payload


class JobStore:
    """Thread-safe job registry with disk persistence."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._jobs: Dict[str, JobRecord] = {}
        self._by_document: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("job store unreadable (%s); starting empty", exc)
            return
        for item in payload.get("jobs", []):
            try:
                record = JobRecord(**item)
            except TypeError:
                continue
            self._jobs[record.job_id] = record
            self._by_document[_document_key(record.organization_id, record.document_id)] = record.job_id

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"jobs": [asdict(record) for record in self._jobs.values()][-500:]}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)

    # ------------------------------------------------------------------ #
    def create(self, **kwargs) -> JobRecord:
        record = JobRecord(job_id=f"job_{uuid.uuid4().hex[:12]}", **kwargs)
        with self._lock:
            self._jobs[record.job_id] = record
            self._by_document[_document_key(record.organization_id, record.document_id)] = record.job_id
            self._persist()
        return record

    def update(self, job_id: str, **changes) -> Optional[JobRecord]:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            for key, value in changes.items():
                setattr(record, key, value)
            record.updated_at = _now()
            self._persist()
            return record

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            return self._jobs.get(job_id)

    def by_document(self, organization_id: str, document_id: str) -> Optional[JobRecord]:
        with self._lock:
            job_id = self._by_document.get(_document_key(organization_id, document_id))
            return self._jobs.get(job_id) if job_id else None

    def list_documents(
        self,
        organization_id: str,
        knowledge_base_id: Optional[str] = None,
        include_deleted: bool = False,
        limit: int = 200,
    ) -> List["JobRecord"]:
        """Documents of one organization, newest first.

        Iterates ``_by_document`` (one entry per document) instead of the job list, so a
        re-indexed document appears once rather than once per attempt.
        """
        prefix = f"{organization_id}::"
        with self._lock:
            records: List[JobRecord] = []
            for key, job_id in self._by_document.items():
                if not key.startswith(prefix):
                    continue
                record = self._jobs.get(job_id)
                if record is None:
                    continue
                if record.organization_id != organization_id:
                    logger.error("job store key/document mismatch for %s", key)
                    continue
                if record.status == STATUS_DELETED and not include_deleted:
                    continue
                if knowledge_base_id and record.knowledge_base_id != knowledge_base_id:
                    continue
                records.append(record)
        records.sort(key=lambda item: item.updated_at or "", reverse=True)
        return records[: max(1, limit)]

    def mark_deleted(self, organization_id: str, document_id: str) -> None:
        with self._lock:
            job_id = self._by_document.get(_document_key(organization_id, document_id))
            record = self._jobs.get(job_id) if job_id else None
            if record is not None:
                record.status = STATUS_DELETED
                record.stage = "deleted"
                record.updated_at = _now()
                self._persist()

    def update_unless_deleted(self, job_id: str, **changes) -> Optional[JobRecord]:
        """Perbarui pekerjaan, kecuali dokumennya sudah dihapus di tengah jalan.

        Ringkasan berjalan di worker terpisah dan bisa selesai SETELAH dokumen dihapus. Tanpa
        penjaga ini, ``update(status=completed)`` akan menghidupkan kembali catatan yang sudah
        ditandai terhapus - dokumen yang sudah dihapus muncul lagi di daftar, padahal isinya
        sudah tidak ada. Bila sudah terhapus, jangan sentuh apa pun.
        """
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.status == STATUS_DELETED:
                return None
            for key, value in changes.items():
                setattr(record, key, value)
            record.updated_at = _now()
            self._persist()
            return record

    def is_deleted(self, job_id: str) -> bool:
        with self._lock:
            record = self._jobs.get(job_id)
            return record is None or record.status == STATUS_DELETED

    def stats(self) -> Dict[str, int]:
        with self._lock:
            counts: Dict[str, int] = {}
            for record in self._jobs.values():
                counts[record.status] = counts.get(record.status, 0) + 1
            return counts


def _document_key(organization_id: str, document_id: str) -> str:
    return f"{organization_id}::{document_id}"


class IndexingPipeline:
    """Document -> vectors. Usable directly (tests, re-index) or via the worker."""

    def __init__(
        self,
        settings: Settings,
        embedder: EmbedderService,
        sparse: SparseIndex,
        jobs: JobStore,
        tables: Optional[TableStore] = None,
        generator=None,
    ) -> None:
        self._settings = settings
        self._embedder = embedder
        self._sparse = sparse
        self._jobs = jobs
        self._tables = tables
        # Dipakai untuk membuat ringkasan knowledge turunan saat dokumen diindeks. Boleh None
        # (mis. pada uji unit): ringkasan dilewati dan alasannya dilaporkan apa adanya.
        self._generator = generator
        # Cara menyerahkan pembuatan ringkasan ke jalur terpisah. Diisi oleh IndexingWorker;
        # None berarti ringkasan dikerjakan di tempat (pipeline dijalankan langsung).
        self._summary_sink = None

    def set_summary_sink(self, sink) -> None:
        """Daftarkan cara mengantrikan ringkasan (dipanggil worker saat start)."""
        self._summary_sink = sink

    # ------------------------------------------------------------------ #
    def run(
        self,
        *,
        job_id: str,
        document_id: str,
        organization_id: str,
        knowledge_base_id: str,
        document_name: str = "",
        file_url: str = "",
        content_base64: str = "",
        text: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        language: str = "",
        replace: bool = True,
        web_url: str = "",
        web_max_pages: int = 0,
        web_max_depth: int = -1,
        web_follow_files: bool = True,
    ) -> JobRecord:
        started = time.perf_counter()
        record = self._jobs.update(job_id, status=STATUS_PROCESSING, stage="parsing", attempts=(self._jobs.get(job_id).attempts + 1 if self._jobs.get(job_id) else 1))
        if record is None:
            raise AppError("INTERNAL_ERROR", f"unknown job {job_id}")

        try:
            # Sumber dari web: satu situs/halaman menjelajah menjadi SATU dokumen, dengan
            # setiap halaman menyimpan alamatnya sendiri sehingga sitasi menunjuk halaman
            # yang benar (bukan sekadar nomor halaman).
            if web_url:
                return self._run_web(
                    started,
                    job_id=job_id,
                    document_id=document_id,
                    organization_id=organization_id,
                    knowledge_base_id=knowledge_base_id,
                    document_name=document_name,
                    web_url=web_url,
                    max_pages=web_max_pages,
                    max_depth=web_max_depth,
                    follow_files=web_follow_files,
                    language=language,
                    metadata=metadata,
                    replace=replace,
                )

            raw, parse_name, display_name = self._load_source(
                file_url=file_url, content_base64=content_base64, text=text, document_name=document_name
            )
            # Two separate rules: the payload is validated on the name that describes its
            # bytes (inline text may be labelled "SOP Cuti 2026" with no extension), while
            # the label itself may not advertise a type we refuse to store ("payload.exe").
            validate_display_label(display_name, self._settings)
            validate_upload(self._settings, parse_name, raw)
            parsed = parse_document(raw, parse_name)
            # Nama tampilan yang benar-benar dipakai (mis. diambil dari nama berkas URL) ikut
            # disimpan, supaya daftar dokumen tidak menampilkan baris tanpa nama.
            if display_name:
                self._jobs.update(job_id, document_name=display_name, source_url=file_url or "")
            self._jobs.update(job_id, stage="chunking", pages=len(parsed.pages))
            metric_incr("documents_indexed")

            # Tabel (xlsx/csv/ods) disimpan TERSTRUKTUR supaya pertanyaan agregat dihitung
            # dari data riil, bukan ditebak LLM dari potongan teks.
            tables_saved = 0
            if self._tables is not None and supports_tables(parse_name):
                try:
                    extracted = extract_tables(raw, parse_name)
                    tables_saved = self._tables.replace_document(
                        organization_id=organization_id,
                        knowledge_base_id=knowledge_base_id,
                        document_id=document_id,
                        document_name=display_name or parse_name,
                        tables=extracted,
                    )
                    if extracted:
                        logger.info(
                            "stored %d table(s) for %s (%d rows)",
                            tables_saved,
                            document_id,
                            sum(table.row_count for table in extracted),
                        )
                except AppError as exc:
                    logger.warning("ekstraksi tabel gagal untuk %s: %s", document_id, exc)
                except Exception as exc:  # noqa: BLE001 - tabel opsional, teks tetap diindeks
                    logger.warning("ekstraksi tabel gagal tak terduga untuk %s: %s", document_id, exc)

            chunks = chunk_document(
                parsed,
                document_id=document_id,
                chunk_size=self._settings.chunk_size,
                chunk_overlap=self._settings.chunk_overlap,
                min_chunk_tokens=self._settings.min_chunk_tokens,
            )
            if not chunks:
                raise AppError("INDEXING_FAILED", "no extractable text found in document")

            self._jobs.update(job_id, stage="embedding")
            vectors = self._encode(chunks, organization_id, knowledge_base_id, document_id, document_name, parsed, file_url, language, metadata)

            # ISI DIINDESKAN DULU, ringkasan MENYUSUL DI JALUR TERPISAH.
            #
            # Sebelumnya ringkasan dibuat di sini juga, dan karena satu-satunya worker dipakai
            # bersama, unggahan kecil menunggu di antrian selama dokumen besar diringkas
            # (map-reduce = puluhan panggilan LLM; di produksi sempat 15 menit). Sekarang isi
            # selesai dan langsung bisa dicari, lalu ringkasan dikerjakan worker lain.
            self._jobs.update(job_id, stage="upserting")
            if replace:
                repository.delete_document(self._settings, organization_id=organization_id, document_id=document_id)
                self._sparse.remove_document(organization_id, knowledge_base_id, document_id)
                # Tabel terdahulu sudah diganti oleh ``replace_document`` saat parse; di sini
                # hanya perlu membresihkan tabel basi bila berkas barunya bukan tabel lagi.
                if self._tables is not None and not supports_tables(parse_name):
                    self._tables.delete_document(organization_id=organization_id, document_id=document_id)
            written = repository.upsert_chunks(self._settings, dim=self._embedder.dim, points=vectors)
            self._sparse.upsert(
                organization_id,
                knowledge_base_id,
                [(chunk.chunk_id, document_id, chunk.content) for chunk in chunks],
            )

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            metric_observe("embedding_latency", duration_ms / 1000.0)
            metric_incr("chunks_created", len(chunks))
            # Status tetap "processing" sampai ringkasan selesai: `completed` berarti pekerjaan
            # benar-benar tuntas, bukan "isinya sudah masuk tapi ringkasannya belum". Yang
            # dipisah adalah ANTRIANNYA, bukan arti statusnya - lihat _queue_summary.
            if self._settings.document_summary_enabled:
                record = self._jobs.update(
                    job_id,
                    stage="summarizing",
                    chunks=written,
                    tables=tables_saved,
                    tokens=sum(chunk.token_count for chunk in chunks),
                    duration_ms=duration_ms,
                )
                self._queue_summary(
                    job_id=job_id,
                    document_id=document_id,
                    organization_id=organization_id,
                    knowledge_base_id=knowledge_base_id,
                    document_name=document_name or parsed.document_name,
                    language=language or parsed.language,
                    source_url=file_url,
                    metadata=metadata,
                )
            else:
                record = self._jobs.update(
                    job_id,
                    status=STATUS_COMPLETED,
                    stage="completed",
                    chunks=written,
                    tables=tables_saved,
                    tokens=sum(chunk.token_count for chunk in chunks),
                    duration_ms=duration_ms,
                    # Fitur ringkasan dimatikan: isi dokumen tetap selesai, dan alasannya
                    # dicatat supaya tidak terlihat seperti ringkasan yang gagal.
                    summary_error="ringkasan dimatikan (DOCUMENT_SUMMARY_ENABLED=false)",
                )
            logger.info(
                "indexed document %s (%d chunks) for org %s", document_id, written, organization_id
            )
            return self._jobs.get(job_id) or record
        except Exception as exc:  # noqa: BLE001
            metric_incr("documents_failed")
            message = exc.message if isinstance(exc, AppError) else str(exc)
            code = exc.code if isinstance(exc, AppError) else "INDEXING_FAILED"
            logger.error("indexing failed for %s: %s", document_id, message)
            return self._jobs.update(
                job_id,
                status=STATUS_FAILED,
                stage="failed",
                error=f"{code}: {message}"[:500],
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )

    # ------------------------------------------------------------------ #
    def _run_web(
        self,
        started: float,
        *,
        job_id: str,
        document_id: str,
        organization_id: str,
        knowledge_base_id: str,
        document_name: str,
        web_url: str,
        max_pages: int,
        max_depth: int,
        follow_files: bool,
        language: str,
        metadata: Optional[Dict[str, Any]],
        replace: bool,
    ) -> JobRecord:
        """Indeks satu situs/halaman web sebagai satu dokumen berhalaman-URL.

        Setiap halaman web menjadi satu "halaman" dokumen dengan ``source_url`` sendiri, jadi
        sitasi menunjuk alamat yang benar dan halaman yang sama tidak terhitung dua kali.
        """
        from app.parsing.web import WebFetchError, crawl

        self._jobs.update(job_id, stage="fetching")
        try:
            result = crawl(
                web_url,
                self._settings,
                max_pages=max_pages or None,
                max_depth=(max_depth if max_depth >= 0 else None),
                follow_files=follow_files,
            )
        except WebFetchError as exc:
            raise AppError("DOCUMENT_NOT_FOUND", f"gagal mengambil {web_url}: {exc}") from exc

        if not result.pages:
            detail = "; ".join(f"{item['url']}: {item['reason']}" for item in result.skipped[:5])
            raise AppError(
                "INDEXING_FAILED",
                f"tidak ada teks yang bisa diambil dari {web_url}" + (f" ({detail})" if detail else ""),
            )

        root = urlparse(web_url)
        host_label = root.hostname or "web"
        display_name = document_name or f"{host_label} ({len(result.pages)} halaman)"
        # Simpan nama yang benar-benar dipakai supaya daftar dokumen tidak tampil kosong.
        self._jobs.update(job_id, document_name=display_name, source_url=web_url)
        pages: List[ParsedPage] = []
        for index, page in enumerate(result.pages, start=1):
            pages.append(
                ParsedPage(
                    page=index,
                    text=page.text,
                    source_url=page.url,
                    title=page.title,
                )
            )
        parsed = ParsedDocument(document_name=display_name, pages=pages, language=language or "id", parser="web")

        self._jobs.update(job_id, stage="chunking", pages=len(pages))
        metric_incr("documents_indexed")
        metric_incr("web_pages_fetched", len(pages))

        chunks = chunk_document(
            parsed,
            document_id=document_id,
            chunk_size=self._settings.chunk_size,
            chunk_overlap=self._settings.chunk_overlap,
            min_chunk_tokens=self._settings.min_chunk_tokens,
        )
        if not chunks:
            raise AppError("INDEXING_FAILED", f"tidak ada teks yang bisa diindeks dari {web_url}")

        page_urls = [page.url for page in result.pages]
        enriched = dict(metadata or {})
        enriched.setdefault("source", "web")
        enriched.setdefault("source_url", web_url)
        enriched["web_pages"] = len(page_urls)
        enriched["web_urls"] = page_urls[:200]
        if result.skipped:
            enriched["web_skipped"] = len(result.skipped)

        self._jobs.update(job_id, stage="embedding")
        vectors = self._encode(
            chunks, organization_id, knowledge_base_id, document_id, display_name, parsed, web_url, language, enriched
        )

        self._jobs.update(job_id, stage="upserting")
        if replace:
            repository.delete_document(self._settings, organization_id=organization_id, document_id=document_id)
            self._sparse.remove_document(organization_id, knowledge_base_id, document_id)
        written = repository.upsert_chunks(self._settings, dim=self._embedder.dim, points=vectors)
        self._sparse.upsert(
            organization_id,
            knowledge_base_id,
            [(chunk.chunk_id, document_id, chunk.content) for chunk in chunks],
        )
        # Sama seperti jalur berkas: isi selesai dulu, ringkasan di jalur terpisah.
        if self._settings.document_summary_enabled:
            self._jobs.update(
                job_id,
                stage="summarizing",
                chunks=written,
                tables=0,
                tokens=sum(chunk.token_count for chunk in chunks),
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            self._queue_summary(
                job_id=job_id,
                document_id=document_id,
                organization_id=organization_id,
                knowledge_base_id=knowledge_base_id,
                document_name=display_name,
                language=language or "id",
                source_url=web_url,
                metadata=enriched,
            )
        else:
            self._jobs.update(
                job_id,
                status=STATUS_COMPLETED,
                stage="completed",
                chunks=written,
                tables=0,
                tokens=sum(chunk.token_count for chunk in chunks),
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                summary_error="ringkasan dimatikan (DOCUMENT_SUMMARY_ENABLED=false)",
            )

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        metric_observe("embedding_latency", duration_ms / 1000.0)
        metric_incr("chunks_created", len(chunks))
        record = self._jobs.get(job_id) or self._jobs.update(job_id, status=STATUS_COMPLETED, stage="completed")
        logger.info(
            "indexed web %s: %d halaman, %d chunk, %d dilewati, org %s",
            web_url,
            len(page_urls),
            written,
            len(result.skipped),
            organization_id,
        )
        return record

    # ------------------------------------------------------------------ #
    def _queue_summary(
        self,
        *,
        job_id: str,
        document_id: str,
        organization_id: str,
        knowledge_base_id: str,
        document_name: str,
        language: str,
        source_url: str,
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        """Serahkan pembuatan ringkasan ke worker ringkasan (jalur terpisah).

        Pipeline tidak tahu soal worker; pemanggil yang punya worker (``IndexingWorker``)
        mengeset ``_summary_sink``. Bila tidak ada (pipeline dijalankan langsung, mis. di uji
        unit), ringkasan dikerjakan di tempat supaya hasilnya tetap ada.
        """
        payload = {
            "job_id": job_id,
            "document_id": document_id,
            "organization_id": organization_id,
            "knowledge_base_id": knowledge_base_id,
            "document_name": document_name,
            "language": language,
            "source_url": source_url,
            "metadata": metadata,
        }
        if not self._settings.document_summary_enabled:
            return
        if self._summary_sink is not None:
            try:
                self._summary_sink(payload)
                return
            except Exception as exc:  # noqa: BLE001 - jalur ringkasan tidak boleh fatal
                logger.warning("gagal mengantrikan ringkasan %s: %s", document_id, exc)
        # Fallback: kerjakan langsung (tanpa worker, atau antrian gagal).
        self.run_summary(**payload)

    # ------------------------------------------------------------------ #
    def run_summary(
        self,
        *,
        job_id: str,
        document_id: str,
        organization_id: str,
        knowledge_base_id: str,
        document_name: str = "",
        language: str = "",
        source_url: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[JobRecord]:
        """Buat ringkasan dokumen yang ISINYA SUDAH terindeks (jalur terpisah).

        Dipanggil oleh worker ringkasan setelah isi dokumen tersimpan, sehingga unggahan lain
        tidak menunggu di antrian pengindeksan. Kegagalan di sini tidak pernah mengubah status
        isi: yang dicatat hanya ``summary_error``.
        """
        from app.rag.summary import summarize_document

        record = self._jobs.get(job_id)
        if record is None:
            logger.warning("ringkasan untuk job %s dilewati: job tidak ada", job_id)
            return None

        if not self._settings.document_summary_enabled:
            # Fitur dimatikan: tutup pekerjaan sebagai selesai, dengan alasan yang jelas.
            self._jobs.update_unless_deleted(
                job_id,
                status=STATUS_COMPLETED,
                stage="completed",
                summary_error="ringkasan dimatikan (DOCUMENT_SUMMARY_ENABLED=false)",
            )
            return self._jobs.get(job_id)

        if self._generator is None:
            self._jobs.update_unless_deleted(
                job_id,
                status=STATUS_COMPLETED,
                stage="completed",
                summary_error="ringkasan tidak dibuat: generator LLM tidak tersedia di jalur indeks",
            )
            return self._jobs.get(job_id)

        try:
            parts = repository.list_document_chunks(
                self._settings,
                organization_id=organization_id,
                document_id=document_id,
                knowledge_base_id=knowledge_base_id,
            )
        except Exception as exc:  # noqa: BLE001 - ringkasan opsional
            self._jobs.update_unless_deleted(
                job_id, status=STATUS_COMPLETED, stage="completed",
                summary_error=f"ringkasan gagal membaca isi: {exc}"[:300],
            )
            return self._jobs.get(job_id)
        if not parts:
            self._jobs.update_unless_deleted(
                job_id, status=STATUS_COMPLETED, stage="completed",
                summary_error="ringkasan dilewati: dokumen tidak punya isi",
            )
            return self._jobs.get(job_id)

        try:
            result = summarize_document(
                generator=self._generator,
                settings=self._settings,
                document_id=document_id,
                document_name=document_name,
                organization_id=organization_id,
                knowledge_base_id=knowledge_base_id,
                language=language,
                parts=parts,
            )
        except Exception as exc:  # noqa: BLE001 - ringkasan opsional
            logger.warning("ringkasan dokumen %s gagal: %s", document_id, exc)
            self._jobs.update_unless_deleted(
                job_id, status=STATUS_COMPLETED, stage="completed",
                summary_error=f"ringkasan gagal: {exc}"[:300],
            )
            return self._jobs.get(job_id)

        if not result.text.strip():
            self._jobs.update_unless_deleted(
                job_id,
                status=STATUS_COMPLETED,
                stage="completed",
                summary_error=(result.error or "model tidak menghasilkan ringkasan")[:300],
            )
            return self._jobs.get(job_id)

        error = result.error
        if not error and result.partial:
            # Ringkasan sebagian tidak boleh terbaca sebagai ringkasan lengkap.
            error = f"ringkasan sebagian - {result.partial}"

        try:
            points = self._summary_points(
                result.text,
                organization_id=organization_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                document_name=document_name,
                language=language,
                source_url=source_url,
                metadata=metadata,
            )
            repository.upsert_chunks(self._settings, dim=self._embedder.dim, points=points)
            self._sparse.upsert(
                organization_id,
                knowledge_base_id,
                [(SUMMARY_CHUNK_ID, document_id, result.text)],
            )
        except Exception as exc:  # noqa: BLE001 - gagal menyimpan ringkasan tidak fatal
            logger.warning("ringkasan dokumen %s gagal disimpan: %s", document_id, exc)
            self._jobs.update_unless_deleted(
                job_id, status=STATUS_COMPLETED, stage="completed",
                summary_error=f"ringkasan gagal disimpan: {exc}"[:300],
            )
            return self._jobs.get(job_id)

        metric_incr("summaries_created")
        logger.info(
            "ringkasan dokumen %s siap: %d token, %d bagian, %d langkah%s",
            document_id,
            result.tokens,
            result.parts,
            result.passes,
            f" (sebagian: {result.partial})" if result.partial else "",
        )
        return self._jobs.update_unless_deleted(
            job_id,
            status=STATUS_COMPLETED,
            stage="completed",
            summary=result.text,
            summary_tokens=result.tokens,
            summary_of=document_id,
            summary_error=error,
        )

    def _summary_points(
        self,
        summary: str,
        *,
        organization_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_name: str,
        language: str,
        source_url: str,
        metadata: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Titik vektor untuk potongan ringkasan (document_id sama dengan dokumen asalnya)."""
        from app.rag.summary import summary_chunk_payload

        payload = summary_chunk_payload(
            summary=summary,
            document_id=document_id,
            document_name=document_name,
            organization_id=organization_id,
            knowledge_base_id=knowledge_base_id,
            language=language,
            source_url=source_url,
            metadata=metadata,
        )
        payload["created_at"] = _now()
        vector = self._embedder.encode([_contextual_text_from_payload(payload)])[0]
        return [{"chunk_id": payload["chunk_id"], "vector": vector, "payload": payload}]

    # ------------------------------------------------------------------ #
    def _encode(
        self,
        chunks,
        organization_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_name: str,
        parsed: ParsedDocument,
        source_url: str,
        language: str,
        metadata: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        created_at = _now()
        points: List[Dict[str, Any]] = []
        batch = self._settings.embedding_batch_size
        contexts = [_contextual_text(chunk) for chunk in chunks]
        for start in range(0, len(chunks), batch):
            window_chunks = chunks[start : start + batch]
            window_texts = contexts[start : start + batch]
            vectors = self._embedder.encode(window_texts)
            for chunk, vector in zip(window_chunks, vectors):
                points.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "vector": vector,
                        "payload": {
                            "document_id": document_id,
                            "organization_id": organization_id,
                            "knowledge_base_id": knowledge_base_id,
                            "chunk_id": chunk.chunk_id,
                            "content": chunk.content,
                            "document_name": document_name or parsed.document_name,
                            "page": chunk.page,
                            "section": chunk.section,
                            # Alamat halaman web potongan ini bila ada; kalau tidak, alamat
                            # dokumennya (mis. berkas publik). Dipakai sitasi + ambil gambar.
                            "source_url": getattr(chunk, "source_url", "") or source_url,
                            "page_title": getattr(chunk, "title", ""),
                            "language": language or parsed.language,
                            "chunk_index": int(chunk.chunk_id.rsplit("_", 1)[-1]),
                            "token_count": chunk.token_count,
                            "is_table": chunk.is_table,
                            "created_at": created_at,
                            "extra": metadata or {},
                        },
                    }
                )
        return points

    def _load_source(
        self,
        *,
        file_url: str,
        content_base64: str,
        text: str,
        document_name: str,
    ) -> tuple[bytes, str, str]:
        """Return ``(bytes, parse_name, display_name)``.

        ``display_name`` is what the KMS calls the document (kept in payloads and
        citations); ``parse_name`` only routes the parser. Inline text is parsed as
        text even when the document is *named* ``SOP Cuti.pdf`` — the name describes
        the source artifact, the payload describes the bytes we actually hold.
        """
        if content_base64:
            try:
                raw = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise AppError("VALIDATION_ERROR", "content_base64 is not valid base64") from exc
            name = document_name or "upload.bin"
            return raw, name, name
        if text:
            display = document_name or "inline.txt"
            stem = Path(display).stem or "inline"
            return text.encode("utf-8"), f"{stem}.txt", display
        if not file_url:
            raise AppError("VALIDATION_ERROR", "one of file_url, content_base64 or text is required")

        source_name = sanitize_filename(file_url.rsplit("/", 1)[-1] or "document")
        name = document_name or source_name
        # The parser is routed by the source's own type; the label is only a label, so a
        # PDF fetched from disk still parses as a PDF when the label omits the extension.
        parse_name = source_name if Path(source_name).suffix else name
        if file_url.startswith("file://") or (len(file_url) > 1 and file_url[1] == ":"):
            raw_path = file_url[7:] if file_url.startswith("file://") else file_url
            # file:///C:/x -> /C:/x on Windows: drop the leading slash before the drive
            if len(raw_path) > 2 and raw_path[0] == "/" and raw_path[2] == ":":
                raw_path = raw_path[1:]
            path = Path(raw_path)
            if not path.exists():
                raise AppError("DOCUMENT_NOT_FOUND", f"local file not found: {path.name}")
            size = path.stat().st_size
            if size > self._settings.max_upload_bytes:
                raise AppError("PAYLOAD_TOO_LARGE", "document exceeds MAX_UPLOAD_MB")
            return path.read_bytes(), path.name, name

        # Berkas publik diambil lewat pengaman URL yang sama dengan crawl web: tanpa ini,
        # satu baris payload bisa menyuruh server membaca jaringan dalam (SSRF).
        from app.core.urlguard import UrlRejected, assert_public_url

        try:
            assert_public_url(
                file_url, allow_private=bool(getattr(self._settings, "allow_private_urls", False))
            )
        except UrlRejected as exc:
            raise AppError("VALIDATION_ERROR", f"file_url ditolak: {exc}") from exc

        import httpx

        try:
            with httpx.stream(
                "GET",
                file_url,
                timeout=self._settings.file_fetch_timeout,
                follow_redirects=True,
                headers={"User-Agent": getattr(self._settings, "web_user_agent", "RAG-Service/1.0")},
            ) as response:
                response.raise_for_status()
                # Pengalihan sudah diikuti httpx; periksa tujuan akhirnya juga, karena itulah
                # jalur klasik untuk lolos dari pemeriksaan URL awal.
                try:
                    assert_public_url(
                        str(response.url),
                        allow_private=bool(getattr(self._settings, "allow_private_urls", False)),
                    )
                except UrlRejected as exc:
                    raise AppError("VALIDATION_ERROR", f"pengalihan file_url ditolak: {exc}") from exc
                declared = int(response.headers.get("content-length") or 0)
                if declared and declared > self._settings.max_upload_bytes:
                    raise AppError("PAYLOAD_TOO_LARGE", "document exceeds MAX_UPLOAD_MB")
                buffer = bytearray()
                for block in response.iter_bytes():
                    buffer.extend(block)
                    if len(buffer) > self._settings.max_upload_bytes:
                        raise AppError("PAYLOAD_TOO_LARGE", "document exceeds MAX_UPLOAD_MB")
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AppError("DOCUMENT_NOT_FOUND", f"could not fetch file_url: {exc}") from exc
        return bytes(buffer), parse_name, name


def _contextual_text(chunk) -> str:
    """Index with a contextual header so isolated chunks stay interpretable."""
    header = " | ".join(part for part in [chunk.section, f"page {chunk.page}"] if part)
    return f"{header}\n{chunk.content}" if header else chunk.content


def _contextual_text_from_payload(payload: Dict[str, Any]) -> str:
    """Sama seperti ``_contextual_text``, untuk payload yang belum jadi objek ``Chunk``."""
    section = str(payload.get("section") or "")
    header = " | ".join(part for part in [section] if part)
    content = str(payload.get("content") or "")
    return f"{header}\n{content}" if header else content


class IndexingWorker:
    """Background asyncio consumer around the CPU-bound pipeline."""

    def __init__(self, settings: Settings, pipeline: IndexingPipeline, jobs: JobStore) -> None:
        self._settings = settings
        self._pipeline = pipeline
        self._jobs = jobs
        self._queue: Optional[asyncio.Queue] = None
        self._tasks: List[asyncio.Task] = []
        self._executor: Optional[ThreadPoolExecutor] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        # Jalur ringkasan dipisah supaya panggilan LLM yang panjang tidak menahan antrian
        # pengindeksan (lihat submit_summary).
        self._summary_queue: Optional[asyncio.Queue] = None
        self._summary_executor: Optional[ThreadPoolExecutor] = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def queued(self) -> int:
        return self._queue.qsize() if self._queue else 0

    async def start(self) -> None:
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._executor = ThreadPoolExecutor(
            max_workers=self._settings.indexing_workers, thread_name_prefix="indexer"
        )
        summary_workers = max(1, int(getattr(self._settings, "summary_workers", 2)))
        self._summary_queue = asyncio.Queue()
        self._summary_executor = ThreadPoolExecutor(max_workers=summary_workers, thread_name_prefix="summarizer")
        self._tasks = [
            asyncio.create_task(self._consume(worker_id), name=f"indexer-{worker_id}")
            for worker_id in range(self._settings.indexing_workers)
        ] + [
            asyncio.create_task(self._consume_summary(worker_id), name=f"summarizer-{worker_id}")
            for worker_id in range(summary_workers)
        ]
        self._running = True
        # Ringkasan dikerjakan di jalur terpisah supaya panggilan LLM yang panjang tidak
        # menahan antrian pengindeksan.
        self._pipeline.set_summary_sink(self.submit_summary)
        logger.info(
            "indexing worker started (%d slot indeks, %d slot ringkasan)",
            self._settings.indexing_workers,
            summary_workers,
        )

    async def stop(self) -> None:
        """Hentikan worker dengan aman: tunggu pekerjaan yang sedang berjalan selesai dulu.

        Ringkasan berjalan di thread terpisah dan menulis ke Qdrant/sparse. Kalau shutdown
        memakai ``wait=False``, thread itu masih menulis ketika pemanggil (mis. fixture uji,
        atau proses yang berhenti) menutup client Qdrant - dan penulisan ke client yang sudah
        ditutup itu membuat proses mati (access violation), bukan sekadar galat Python.
        Karena itu: hentikan penerimaan pekerjaan baru, batalkan yang masih menunggu di antrian,
        lalu TUNGGU yang sedang berjalan benar-benar selesai.
        """
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        # Buang pekerjaan yang belum sempat mulai supaya tidak menunggu tanpa guna.
        for queue in (self._queue, self._summary_queue):
            if queue is not None:
                while not queue.empty():
                    try:
                        queue.get_nowait()
                        queue.task_done()
                    except asyncio.QueueEmpty:  # pragma: no cover - balapan antrian
                        break
        for executor in (self._executor, self._summary_executor):
            if executor is not None:
                # wait=True: pekerjaan yang sedang jalan diselesaikan sebelum kita lanjut.
                executor.shutdown(wait=True, cancel_futures=True)
        self._executor = None
        self._summary_executor = None
        self._queue = None
        self._summary_queue = None

    def submit(self, payload: Dict[str, Any]) -> JobRecord:
        record = self._jobs.create(**{key: payload[key] for key in _JOB_FIELDS if key in payload})
        self._jobs.update(
            record.job_id,
            source_url=payload.get("web_url") or payload.get("file_url", ""),
        )
        if not self._running or self._queue is None or self._loop is None:
            logger.warning("worker not running; running job %s inline", record.job_id)
            self._pipeline.run(job_id=record.job_id, **payload)
            return self._jobs.get(record.job_id) or record
        asyncio.run_coroutine_threadsafe(self._queue.put((record.job_id, payload)), self._loop)
        return record

    async def _consume(self, worker_id: int) -> None:
        assert self._queue is not None and self._executor is not None
        while True:
            job_id, payload = await self._queue.get()
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    self._executor,
                    lambda: self._pipeline.run(job_id=job_id, **payload),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("worker %d crashed on job %s: %s", worker_id, job_id, exc)
                self._jobs.update(job_id, status=STATUS_FAILED, stage="failed", error=str(exc)[:500])
            finally:
                self._queue.task_done()

    def stats(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "workers": self._settings.indexing_workers,
            "queued": self.queued,
            "summary_queued": self.summary_queued,
            "summary_workers": self._settings.summary_workers,
            "jobs": self._jobs.stats(),
        }

    # ------------------------------------------------------------------ #
    # Ringkasan: jalur TERPISAH dari pengindeksan
    # ------------------------------------------------------------------ #
    def submit_summary(self, payload: Dict[str, Any]) -> None:
        """Antrikan pembuatan ringkasan untuk satu dokumen yang isinya sudah terindeks.

        Dipisah dari antrian pengindeksan dengan sengaja. Ringkasan memanggil LLM - puluhan
        kali untuk dokumen besar - sehingga kalau dikerjakan di worker yang sama, setiap
        unggahan berikutnya menunggu di antrian. Isi dokumen tidak boleh tersandera oleh
        ringkasannya: isi sudah tersimpan saat fungsi ini dipanggil.
        """
        if not self._running or self._summary_queue is None or self._loop is None:
            # Tanpa worker (mis. uji unit yang menjalankan pipeline langsung): ringkasan
            # dikerjakan di tempat supaya hasilnya tetap ada.
            self._pipeline.run_summary(**payload)
            return
        asyncio.run_coroutine_threadsafe(self._summary_queue.put(payload), self._loop)

    @property
    def summary_queued(self) -> int:
        return self._summary_queue.qsize() if self._summary_queue else 0

    async def _consume_summary(self, worker_id: int) -> None:
        assert self._summary_queue is not None and self._summary_executor is not None
        while True:
            payload = await self._summary_queue.get()
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    self._summary_executor,
                    lambda: self._pipeline.run_summary(**payload),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - ringkasan opsional
                document_id = str(payload.get("document_id") or "")
                logger.error("ringkasan untuk %s gagal di worker %d: %s", document_id, worker_id, exc)
                try:
                    self._jobs.update(
                        str(payload.get("job_id") or ""),
                        status=STATUS_COMPLETED,
                        stage="completed",
                        summary_error=f"ringkasan gagal: {exc}"[:300],
                    )
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self._summary_queue.task_done()


_JOB_FIELDS = (
    "document_id",
    "organization_id",
    "knowledge_base_id",
    "document_name",
    "source_url",
)


def total_tokens(chunks) -> int:
    return sum(estimate_tokens(chunk.content) for chunk in chunks)
