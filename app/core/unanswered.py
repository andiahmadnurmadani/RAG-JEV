"""Pertanyaan yang tidak terjawab oleh RAG — dikumpulkan supaya celah knowledge terlihat.

Setiap kali /query berakhir "tidak ditemukan" (alasan yang dipilih operator), pertanyaannya
dicatat per organisasi + knowledge base. Pertanyaan yang sama (setelah dinormalkan) tidak
dicatat berulang: hitungannya yang naik, sehingga daftar langsung menunjukkan apa yang paling
sering dicari tetapi belum ada di knowledge.

Disimpan di SQLite di samping ``tables.sqlite`` (volume data yang sama), dengan retensi:
catatan lebih tua dari ``unanswered_retention_days`` dan kelebihan di atas
``unanswered_max_entries`` per organisasi dibuang otomatis.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.core.logging import get_logger

logger = get_logger(__name__)

STATUSES = ("open", "resolved")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS unanswered (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    query TEXT NOT NULL,
    query_key TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    best_score REAL,
    application_id TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'open',
    note TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE (organization_id, knowledge_base_id, query_key)
);
CREATE INDEX IF NOT EXISTS idx_unanswered_scope
    ON unanswered (organization_id, knowledge_base_id, status, last_seen);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def query_key(query: str) -> str:
    """Bentuk pembanding: huruf kecil, spasi dirapikan, tanda baca di ujung dibuang."""
    text = " ".join(str(query or "").lower().split())
    return re.sub(r"^[\W_]+|[\W_]+$", "", text)


def store_path(settings: Any) -> str:
    configured = str(getattr(settings, "unanswered_store_path", "") or "").strip()
    if configured:
        return configured
    return str(Path(settings.table_store_path).with_name("unanswered.sqlite"))


class UnansweredStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self._path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            try:
                self._connection.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:  # pragma: no cover
                logger.warning("WAL tidak aktif untuk %s", self._path)
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------ #
    def record(
        self,
        *,
        organization_id: str,
        knowledge_base_id: str,
        query: str,
        reason: str,
        best_score: Optional[float],
        application_id: str = "",
        retention_days: int = 90,
        max_entries: int = 5000,
    ) -> None:
        key = query_key(query)
        if not key or not organization_id:
            return
        now = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO unanswered (organization_id, knowledge_base_id, query, query_key, reason,
                                        best_score, application_id, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (organization_id, knowledge_base_id, query_key) DO UPDATE SET
                    count = count + 1,
                    query = excluded.query,
                    reason = excluded.reason,
                    best_score = excluded.best_score,
                    application_id = excluded.application_id,
                    last_seen = excluded.last_seen,
                    status = 'open'
                """,
                (
                    organization_id,
                    knowledge_base_id or "",
                    str(query).strip()[:2000],
                    key[:500],
                    reason or "",
                    None if best_score is None else round(float(best_score), 4),
                    application_id or "",
                    now,
                    now,
                ),
            )
            self._purge(organization_id, retention_days, max_entries)
            self._connection.commit()

    def _purge(self, organization_id: str, retention_days: int, max_entries: int) -> None:
        if retention_days and retention_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=int(retention_days))).isoformat(timespec="seconds")
            self._connection.execute(
                "DELETE FROM unanswered WHERE organization_id = ? AND last_seen < ?",
                (organization_id, cutoff),
            )
        if max_entries and max_entries > 0:
            self._connection.execute(
                """
                DELETE FROM unanswered WHERE organization_id = ? AND id NOT IN (
                    SELECT id FROM unanswered WHERE organization_id = ?
                    ORDER BY last_seen DESC LIMIT ?
                )
                """,
                (organization_id, organization_id, int(max_entries)),
            )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _scope(
        organization_id: str,
        knowledge_base_id: Optional[str],
        allowed: Sequence[str],
        status: Optional[str] = None,
        search: str = "",
    ) -> tuple[str, List[Any]]:
        clauses = ["organization_id = ?"]
        params: List[Any] = [organization_id]
        if knowledge_base_id:
            clauses.append("knowledge_base_id = ?")
            params.append(knowledge_base_id)
        if allowed:
            clauses.append("knowledge_base_id IN (%s)" % ",".join("?" for _ in allowed))
            params.extend(allowed)
        if status in STATUSES:
            clauses.append("status = ?")
            params.append(status)
        if search:
            clauses.append("query_key LIKE ?")
            params.append("%" + query_key(search).replace("%", "").replace("_", "") + "%")
        return " AND ".join(clauses), params

    def list(
        self,
        *,
        organization_id: str,
        knowledge_base_id: Optional[str] = None,
        allowed: Sequence[str] = (),
        status: Optional[str] = None,
        search: str = "",
        sort: str = "recent",
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        where, params = self._scope(organization_id, knowledge_base_id, allowed, status, search)
        order = "count DESC, last_seen DESC" if sort == "frequent" else "last_seen DESC"
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM unanswered WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, int(limit), int(offset)),
            ).fetchall()
            total = self._connection.execute(f"SELECT COUNT(*) FROM unanswered WHERE {where}", params).fetchone()[0]
            base_where, base_params = self._scope(organization_id, knowledge_base_id, allowed)
            counts = {
                row["status"]: row["n"]
                for row in self._connection.execute(
                    f"SELECT status, COUNT(*) AS n FROM unanswered WHERE {base_where} GROUP BY status", base_params
                )
            }
            org_where, org_params = self._scope(organization_id, None, allowed)
            bases = [
                row["knowledge_base_id"]
                for row in self._connection.execute(
                    f"SELECT DISTINCT knowledge_base_id FROM unanswered WHERE {org_where} ORDER BY knowledge_base_id",
                    org_params,
                )
            ]
        return {
            "items": [dict(row) for row in rows],
            "total": int(total),
            "open": int(counts.get("open", 0)),
            "resolved": int(counts.get("resolved", 0)),
            "knowledge_bases": bases,
        }

    def update(
        self,
        *,
        organization_id: str,
        ids: Sequence[int],
        allowed: Sequence[str] = (),
        status: Optional[str] = None,
        note: Optional[str] = None,
    ) -> int:
        if not ids or (status is None and note is None):
            return 0
        sets: List[str] = []
        params: List[Any] = []
        if status in STATUSES:
            sets.append("status = ?")
            params.append(status)
        if note is not None:
            sets.append("note = ?")
            params.append(str(note)[:1000])
        if not sets:
            return 0
        where, scope_params = self._scope(organization_id, None, allowed)
        marks = ",".join("?" for _ in ids)
        with self._lock:
            cursor = self._connection.execute(
                f"UPDATE unanswered SET {', '.join(sets)} WHERE {where} AND id IN ({marks})",
                (*params, *scope_params, *[int(item) for item in ids]),
            )
            self._connection.commit()
            return cursor.rowcount

    def delete(self, *, organization_id: str, ids: Sequence[int], allowed: Sequence[str] = ()) -> int:
        if not ids:
            return 0
        where, params = self._scope(organization_id, None, allowed)
        marks = ",".join("?" for _ in ids)
        with self._lock:
            cursor = self._connection.execute(
                f"DELETE FROM unanswered WHERE {where} AND id IN ({marks})",
                (*params, *[int(item) for item in ids]),
            )
            self._connection.commit()
            return cursor.rowcount

    def delete_matching(
        self,
        *,
        organization_id: str,
        knowledge_base_id: Optional[str] = None,
        allowed: Sequence[str] = (),
        status: Optional[str] = None,
    ) -> int:
        where, params = self._scope(organization_id, knowledge_base_id, allowed, status)
        with self._lock:
            cursor = self._connection.execute(f"DELETE FROM unanswered WHERE {where}", params)
            self._connection.commit()
            return cursor.rowcount

    def delete_knowledge_base(self, *, organization_id: str, knowledge_base_id: str) -> int:
        return self.delete_matching(organization_id=organization_id, knowledge_base_id=knowledge_base_id)
