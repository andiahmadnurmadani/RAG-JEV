"""Log permintaan HTTP yang bisa dibaca dari konsol: siapa memanggil apa, dari mana, hasilnya apa.

Log JSON di stdout (``app.access``) bagus untuk mesin, tetapi tidak bisa dijawab cepat dari
layar: "aplikasi mana yang membanjiri layanan?", "dari situs mana permintaan ini?", "berapa
yang ditolak 429 sejam terakhir?". Penyimpanan ini menjawabnya.

* Disimpan di SQLite di samping ``tables.sqlite`` (volume data yang sama), jadi tidak hilang
  saat container di-restart.
* Penulisan TIDAK dilakukan di jalur permintaan: middleware hanya menaruh catatan ke antrean,
  thread penulis menyimpannya berkelompok. Antrean penuh = catatan dibuang (dan dihitung),
  bukan permintaan yang melambat.
* Retensi: catatan lebih tua dari ``request_log_retention_days`` dan kelebihan di atas
  ``request_log_max_entries`` dibuang otomatis.
* Yang disimpan hanya pengenal (ID kunci, 8 karakter awal sesi), tidak pernah nilai kunci
  atau token; parameter query yang mirip rahasia sudah disamarkan sebelum sampai ke sini.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)

FIELDS = (
    "ts",
    "request_id",
    "method",
    "path",
    "query",
    "status",
    "latency_ms",
    "client_ip",
    "country",
    "host",
    "origin",
    "referer",
    "user_agent",
    "organization_id",
    "application_id",
    "user_id",
    "credential",
    "key_id",
    "session",
    "knowledge_base_id",
    "detail",
    "error_code",
    "request_bytes",
    "response_bytes",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    query TEXT,
    status INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL,
    client_ip TEXT,
    country TEXT,
    host TEXT,
    origin TEXT,
    referer TEXT,
    user_agent TEXT,
    organization_id TEXT,
    application_id TEXT,
    user_id TEXT,
    credential TEXT,
    key_id TEXT,
    session TEXT,
    knowledge_base_id TEXT,
    detail TEXT,
    error_code TEXT,
    request_bytes INTEGER,
    response_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests (ts);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests (status, ts);
CREATE INDEX IF NOT EXISTS idx_requests_source ON requests (client_ip, key_id, ts);
"""

STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx", "429", "errors")

# Kolom yang ikut dicari oleh kotak pencarian bebas.
_SEARCH_COLUMNS = (
    "path",
    "query",
    "client_ip",
    "origin",
    "referer",
    "user_agent",
    "application_id",
    "user_id",
    "key_id",
    "knowledge_base_id",
    "request_id",
    "detail",
    "error_code",
    "host",
)

_QUEUE_SIZE = 10000
_BATCH = 500
_PURGE_EVERY_SECONDS = 60.0


def annotate(request: Any, *, knowledge_base_id: Any = None, detail: Any = None) -> None:
    """Tandai permintaan dengan KB dan keterangan singkat untuk log (dibaca middleware).

    Dipanggil paling awal di route, SEBELUM pembatas permintaan, supaya permintaan yang ditolak
    429 pun tercatat lengkap (KB mana, pertanyaan apa)."""
    state = getattr(request, "state", None)
    if state is None:
        return
    if knowledge_base_id:
        state.log_knowledge_base_id = str(knowledge_base_id)
    if detail:
        state.log_detail = " ".join(str(detail).split())[:300]


def store_path(settings: Any) -> str:
    configured = str(getattr(settings, "request_log_path", "") or "").strip()
    if configured:
        return configured
    return str(Path(settings.table_store_path).with_name("request_log.sqlite"))


def _filters(
    *,
    since: Optional[float],
    status: Optional[str],
    search: str,
    client_ip: Optional[str],
    key_id: Optional[str],
    application_id: Optional[str],
    path: Optional[str],
) -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if since:
        clauses.append("ts >= ?")
        params.append(float(since))
    if status == "errors":
        clauses.append("status >= 400")
    elif status == "429":
        clauses.append("status = 429")
    elif status in ("2xx", "3xx", "4xx", "5xx"):
        low = int(status[0]) * 100
        clauses.append("status >= ? AND status < ?")
        params.extend([low, low + 100])
    if client_ip:
        clauses.append("client_ip = ?")
        params.append(client_ip)
    if key_id:
        clauses.append("key_id = ?")
        params.append(key_id)
    if application_id:
        clauses.append("application_id = ?")
        params.append(application_id)
    if path:
        clauses.append("path = ?")
        params.append(path)
    text = " ".join(str(search or "").split())[:200]
    if text:
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in _SEARCH_COLUMNS) + ")")
        params.extend([pattern] * len(_SEARCH_COLUMNS))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


class RequestLogStore:
    def __init__(self, path: str, *, retention_days: int = 7, max_entries: int = 200000) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = int(retention_days)
        self.max_entries = int(max_entries)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self._path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            try:
                self._connection.execute("PRAGMA journal_mode=WAL")
                self._connection.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError:  # pragma: no cover
                logger.warning("WAL tidak aktif untuk %s", self._path)
            self._connection.commit()
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=_QUEUE_SIZE)
        self.dropped = 0
        self._last_purge = 0.0
        self._closed = False
        self._writer = threading.Thread(target=self._run, name="request-log-writer", daemon=True)
        self._writer.start()

    # ------------------------------------------------------------------ tulis
    def record(self, entry: Dict[str, Any]) -> None:
        """Dipanggil dari middleware: tidak pernah menunggu disk."""
        if self._closed:
            return
        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            batch = [item]
            while len(batch) < _BATCH:
                try:
                    more = self._queue.get_nowait()
                except queue.Empty:
                    break
                if more is None:
                    self._write(batch)
                    return
                batch.append(more)
            self._write(batch)

    def _write(self, batch: Sequence[Dict[str, Any]]) -> None:
        rows = [tuple(entry.get(field) for field in FIELDS) for entry in batch]
        placeholders = ", ".join("?" for _ in FIELDS)
        try:
            with self._lock:
                self._connection.executemany(
                    f"INSERT INTO requests ({', '.join(FIELDS)}) VALUES ({placeholders})", rows
                )
                if time.time() - self._last_purge >= _PURGE_EVERY_SECONDS:
                    self._purge()
                self._connection.commit()
        except sqlite3.DatabaseError as exc:  # log permintaan tidak boleh menjatuhkan layanan
            logger.warning("gagal menyimpan %d catatan log permintaan: %s", len(rows), exc)

    def _purge(self) -> None:
        self._last_purge = time.time()
        if self.retention_days > 0:
            self._connection.execute("DELETE FROM requests WHERE ts < ?", (time.time() - self.retention_days * 86400,))
        if self.max_entries > 0:
            self._connection.execute(
                "DELETE FROM requests WHERE id <= (SELECT id FROM requests ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (self.max_entries,),
            )

    def flush(self, timeout: float = 5.0) -> None:
        """Tunggu antrean kosong (dipakai tes dan sebelum membaca hasil terbaru)."""
        deadline = time.time() + timeout
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.01)
        # satu kelompok terakhir mungkin sedang ditulis
        with self._lock:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(None, timeout=2.0)
        except queue.Full:  # pragma: no cover
            pass
        self._writer.join(timeout=5.0)
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------ baca
    def list(
        self,
        *,
        since: Optional[float] = None,
        status: Optional[str] = None,
        search: str = "",
        client_ip: Optional[str] = None,
        key_id: Optional[str] = None,
        application_id: Optional[str] = None,
        path: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        after_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """``after_id`` hanya mengambil baris yang lebih baru (mode Live di konsol); ``total``
        lalu berarti jumlah baris baru yang cocok, bukan seluruh rentang."""
        where, params = _filters(
            since=since,
            status=status,
            search=search,
            client_ip=client_ip,
            key_id=key_id,
            application_id=application_id,
            path=path,
        )
        if after_id:
            where = (where + " AND" if where else " WHERE") + " id > ?"
            params.append(int(after_id))
        with self._lock:
            # dibaca dalam kunci yang sama: baris yang masuk sesudahnya pasti punya id > last_id
            last_id = self._connection.execute("SELECT MAX(id) FROM requests").fetchone()[0]
            total = self._connection.execute(f"SELECT COUNT(*) FROM requests{where}", params).fetchone()[0]
            rows = self._connection.execute(
                f"SELECT id, {', '.join(FIELDS)} FROM requests{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, int(limit), int(offset)],
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": int(total), "last_id": int(last_id or 0)}

    def summary(
        self,
        *,
        since: float,
        bucket_seconds: int,
        status: Optional[str] = None,
        search: str = "",
        client_ip: Optional[str] = None,
        key_id: Optional[str] = None,
        application_id: Optional[str] = None,
        path: Optional[str] = None,
        top: int = 10,
    ) -> Dict[str, Any]:
        where, params = _filters(
            since=since,
            status=status,
            search=search,
            client_ip=client_ip,
            key_id=key_id,
            application_id=application_id,
            path=path,
        )
        bucket = max(1, int(bucket_seconds))
        with self._lock:
            run = self._connection.execute
            totals = run(
                f"""SELECT COUNT(*) AS total,
                           SUM(CASE WHEN status >= 200 AND status < 400 THEN 1 ELSE 0 END) AS ok,
                           SUM(CASE WHEN status = 429 THEN 1 ELSE 0 END) AS rate_limited,
                           SUM(CASE WHEN status >= 400 AND status < 500 AND status != 429 THEN 1 ELSE 0 END) AS client_errors,
                           SUM(CASE WHEN status >= 500 THEN 1 ELSE 0 END) AS server_errors,
                           AVG(latency_ms) AS avg_latency_ms,
                           MAX(latency_ms) AS max_latency_ms,
                           COUNT(DISTINCT client_ip) AS clients
                    FROM requests{where}""",
                params,
            ).fetchone()
            series = run(
                f"""SELECT CAST(ts / ? AS INTEGER) * ? AS t, COUNT(*) AS count,
                           SUM(CASE WHEN status = 429 THEN 1 ELSE 0 END) AS rate_limited,
                           SUM(CASE WHEN status >= 400 AND status != 429 THEN 1 ELSE 0 END) AS errors
                    FROM requests{where} GROUP BY t ORDER BY t""",
                [bucket, bucket, *params],
            ).fetchall()
            sources = run(
                f"""SELECT client_ip, key_id, application_id, user_id, credential,
                           MAX(country) AS country, MAX(user_agent) AS user_agent,
                           MAX(COALESCE(origin, referer)) AS origin,
                           COUNT(*) AS count,
                           SUM(CASE WHEN status = 429 THEN 1 ELSE 0 END) AS rate_limited,
                           SUM(CASE WHEN status >= 400 AND status != 429 THEN 1 ELSE 0 END) AS errors,
                           AVG(latency_ms) AS avg_latency_ms,
                           MAX(ts) AS last_seen
                    FROM requests{where}
                    GROUP BY client_ip, key_id, application_id, user_id, credential
                    ORDER BY count DESC LIMIT ?""",
                [*params, int(top)],
            ).fetchall()
            paths = run(
                f"""SELECT method, path, COUNT(*) AS count,
                           SUM(CASE WHEN status = 429 THEN 1 ELSE 0 END) AS rate_limited,
                           SUM(CASE WHEN status >= 400 AND status != 429 THEN 1 ELSE 0 END) AS errors,
                           AVG(latency_ms) AS avg_latency_ms
                    FROM requests{where}
                    GROUP BY method, path ORDER BY count DESC LIMIT ?""",
                [*params, int(top)],
            ).fetchall()
            latencies = [
                row[0]
                for row in run(
                    f"SELECT latency_ms FROM requests{where} AND latency_ms IS NOT NULL ORDER BY latency_ms"
                    if where
                    else "SELECT latency_ms FROM requests WHERE latency_ms IS NOT NULL ORDER BY latency_ms",
                    params,
                ).fetchall()
            ]
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
        data = dict(totals) if totals else {}
        for key in ("ok", "rate_limited", "client_errors", "server_errors"):
            data[key] = int(data.get(key) or 0)
        data["total"] = int(data.get("total") or 0)
        data["clients"] = int(data.get("clients") or 0)
        data["p95_latency_ms"] = None if p95 is None else round(float(p95), 2)
        for key in ("avg_latency_ms", "max_latency_ms"):
            data[key] = None if data.get(key) is None else round(float(data[key]), 2)
        return {
            "totals": data,
            "bucket_seconds": bucket,
            "series": [dict(row) for row in series],
            "sources": [dict(row) for row in sources],
            "paths": [dict(row) for row in paths],
            "dropped": self.dropped,
        }

    def clear(self) -> int:
        with self._lock:
            deleted = self._connection.execute("DELETE FROM requests").rowcount
            self._connection.commit()
        return int(deleted or 0)
