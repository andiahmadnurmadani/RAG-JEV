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

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.metrics import incr as metric_incr
from app.core.metrics import observe as metric_observe
from app.core.security import sanitize_filename, validate_display_label, validate_upload
from app.parsing.parser import ParsedDocument, parse_document
from app.qdrant import repository
from app.rag.chunker import chunk_document, estimate_tokens
from app.rag.embedder import EmbedderService
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
    ) -> None:
        self._settings = settings
        self._embedder = embedder
        self._sparse = sparse
        self._jobs = jobs
        self._tables = tables

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
    ) -> JobRecord:
        started = time.perf_counter()
        record = self._jobs.update(job_id, status=STATUS_PROCESSING, stage="parsing", attempts=(self._jobs.get(job_id).attempts + 1 if self._jobs.get(job_id) else 1))
        if record is None:
            raise AppError("INTERNAL_ERROR", f"unknown job {job_id}")

        try:
            raw, parse_name, display_name = self._load_source(
                file_url=file_url, content_base64=content_base64, text=text, document_name=document_name
            )
            # Two separate rules: the payload is validated on the name that describes its
            # bytes (inline text may be labelled "SOP Cuti 2026" with no extension), while
            # the label itself may not advertise a type we refuse to store ("payload.exe").
            validate_display_label(display_name, self._settings)
            validate_upload(self._settings, parse_name, raw)
            parsed = parse_document(raw, parse_name)
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
            record = self._jobs.update(
                job_id,
                status=STATUS_COMPLETED,
                stage="completed",
                chunks=written,
                tables=tables_saved,
                tokens=sum(chunk.token_count for chunk in chunks),
                duration_ms=duration_ms,
            )
            logger.info(
                "indexed document %s (%d chunks) for org %s", document_id, written, organization_id
            )
            return record
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
                            "source_url": source_url,
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

        import httpx

        try:
            with httpx.stream("GET", file_url, timeout=self._settings.file_fetch_timeout, follow_redirects=True) as response:
                response.raise_for_status()
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
        self._tasks = [
            asyncio.create_task(self._consume(worker_id), name=f"indexer-{worker_id}")
            for worker_id in range(self._settings.indexing_workers)
        ]
        self._running = True
        logger.info("indexing worker started (%d slots)", self._settings.indexing_workers)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def submit(self, payload: Dict[str, Any]) -> JobRecord:
        record = self._jobs.create(**{key: payload[key] for key in _JOB_FIELDS if key in payload})
        self._jobs.update(record.job_id, source_url=payload.get("file_url", ""))
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
            "jobs": self._jobs.stats(),
        }


_JOB_FIELDS = (
    "document_id",
    "organization_id",
    "knowledge_base_id",
    "document_name",
    "source_url",
)


def total_tokens(chunks) -> int:
    return sum(estimate_tokens(chunk.content) for chunk in chunks)
