"""Penyimpanan tabel terstruktur (SQLite) untuk perhitungan deterministik.

Mengapa SQLite, bukan JSON seperti ``jobs.json``/sparse index: pertanyaan agregat harus
dihitung dari SELURUH baris, jadi baris perlu bisa dibaca per-dokumen tanpa memuat seluruh
berkas. Dengan ``sqlite3`` satu dokumen bisa dihapus/diganti secara transaksional, dan
pencarian tabel per (organisasi, knowledge base) memakai indeks -- bukan pemindaian berkas.

Isolasi tenant mengikuti aturan yang sama dengan lapisan lain: setiap kueri WAJIB membawa
``organization_id`` dari konteks terpercaya; tidak ada metode yang bisa membaca lintas tenant.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.core.errors import AppError
from app.core.logging import get_logger
from app.parsing.tables import TableData

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tables (
    table_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    document_id     TEXT NOT NULL,
    document_name   TEXT NOT NULL,
    sheet           TEXT NOT NULL,
    headers         TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    truncated       INTEGER NOT NULL DEFAULT 0,
    notes           TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tables_scope ON tables (organization_id, knowledge_base_id);
CREATE INDEX IF NOT EXISTS idx_tables_document ON tables (organization_id, document_id);
CREATE TABLE IF NOT EXISTS table_rows (
    table_id    INTEGER NOT NULL,
    row_index   INTEGER NOT NULL,
    values_json TEXT NOT NULL,
    PRIMARY KEY (table_id, row_index)
);
"""


@dataclass
class StoredTable:
    table_id: int
    organization_id: str
    knowledge_base_id: str
    document_id: str
    document_name: str
    sheet: str
    headers: List[str]
    row_count: int
    truncated: bool = False
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "table_id": self.table_id,
            "document_id": self.document_id,
            "document_name": self.document_name,
            "knowledge_base_id": self.knowledge_base_id,
            "sheet": self.sheet,
            "headers": list(self.headers),
            "row_count": self.row_count,
            "truncated": self.truncated,
            # Catatan dari pembaca tabel (judul lembar yang dilewati, baris/kolom yang
            # dipotong). Diteruskan ke API supaya pemotongan data tidak pernah senyap.
            "notes": list(self.notes),
        }


class TableStore:
    """Registry tabel + barisnya, satu berkas SQLite per lingkungan."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self._path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._migrate()
            try:
                self._connection.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:  # pragma: no cover - sistem berkas aneh
                logger.warning("WAL tidak aktif untuk %s", self._path)
            self._connection.commit()

    def _migrate(self) -> None:
        """Tambahkan kolom baru pada basis data lama tanpa menghapus isinya."""

        columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(tables)")}
        if "notes" not in columns:
            self._connection.execute("ALTER TABLE tables ADD COLUMN notes TEXT NOT NULL DEFAULT '[]'")
            self._connection.commit()
            logger.info("kolom notes ditambahkan ke %s", self._path)

    # ------------------------------------------------------------------ #
    # Tulis
    # ------------------------------------------------------------------ #
    def replace_document(
        self,
        *,
        organization_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_name: str,
        tables: Sequence[TableData],
    ) -> int:
        """Ganti seluruh tabel milik satu dokumen (idempoten saat dokumen diindeks ulang)."""

        if not organization_id or not document_id:
            raise AppError("INDEXING_FAILED", "table store butuh organization_id dan document_id")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                self._delete_document_cursor(cursor, organization_id, document_id)
                for table in tables:
                    cursor.execute(
                        "INSERT INTO tables (organization_id, knowledge_base_id, document_id, "
                        "document_name, sheet, headers, row_count, truncated, notes, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            organization_id,
                            knowledge_base_id,
                            document_id,
                            document_name,
                            table.sheet,
                            json.dumps(list(table.headers), ensure_ascii=False),
                            table.row_count,
                            1 if table.truncated else 0,
                            json.dumps(list(getattr(table, "notes", []) or []), ensure_ascii=False),
                            now,
                        ),
                    )
                    table_id = int(cursor.lastrowid or 0)
                    cursor.executemany(
                        "INSERT INTO table_rows (table_id, row_index, values_json) VALUES (?, ?, ?)",
                        [
                            (table_id, index, json.dumps(list(row), ensure_ascii=False))
                            for index, row in enumerate(table.rows)
                        ],
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return len(tables)

    def delete_document(self, *, organization_id: str, document_id: str) -> int:
        with self._lock:
            cursor = self._connection.cursor()
            removed = self._delete_document_cursor(cursor, organization_id, document_id)
            self._connection.commit()
        return removed

    def _delete_document_cursor(self, cursor: sqlite3.Cursor, organization_id: str, document_id: str) -> int:
        cursor.execute(
            "SELECT table_id FROM tables WHERE organization_id = ? AND document_id = ?",
            (organization_id, document_id),
        )
        ids = [int(row["table_id"]) for row in cursor.fetchall()]
        for table_id in ids:
            cursor.execute("DELETE FROM table_rows WHERE table_id = ?", (table_id,))
        cursor.execute(
            "DELETE FROM tables WHERE organization_id = ? AND document_id = ?",
            (organization_id, document_id),
        )
        return len(ids)

    # ------------------------------------------------------------------ #
    # Baca
    # ------------------------------------------------------------------ #
    def list_tables(
        self,
        *,
        organization_id: str,
        knowledge_base_id: Optional[str] = None,
        document_ids: Optional[Sequence[str]] = None,
    ) -> List[StoredTable]:
        if not organization_id:
            raise AppError("TENANT_CONTEXT_MISSING", "organization_id wajib untuk membaca tabel")
        query = "SELECT * FROM tables WHERE organization_id = ?"
        params: List[Any] = [organization_id]
        if knowledge_base_id:
            query += " AND knowledge_base_id = ?"
            params.append(knowledge_base_id)
        ids = [str(item) for item in (document_ids or []) if item]
        if ids:
            query += f" AND document_id IN ({','.join('?' for _ in ids)})"
            params.extend(ids)
        query += " ORDER BY document_id, table_id"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [self._to_table(row) for row in rows]

    def load_rows(self, table_id: int) -> List[List[str]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT values_json FROM table_rows WHERE table_id = ? ORDER BY row_index",
                (int(table_id),),
            ).fetchall()
        out: List[List[str]] = []
        for row in rows:
            try:
                values = json.loads(row["values_json"])
            except Exception:  # noqa: BLE001 - baris rusak tidak boleh mematikan perhitungan
                continue
            out.append([str(value) for value in values] if isinstance(values, list) else [])
        return out

    def stats(self) -> Dict[str, int]:
        with self._lock:
            tables = int(self._connection.execute("SELECT COUNT(*) FROM tables").fetchone()[0])
            rows = int(self._connection.execute("SELECT COUNT(*) FROM table_rows").fetchone()[0])
        return {"tables": tables, "rows": rows}

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_table(row: sqlite3.Row) -> StoredTable:
        try:
            headers = json.loads(row["headers"])
        except Exception:  # noqa: BLE001
            headers = []
        return StoredTable(
            table_id=int(row["table_id"]),
            organization_id=row["organization_id"],
            knowledge_base_id=row["knowledge_base_id"],
            document_id=row["document_id"],
            document_name=row["document_name"],
            sheet=row["sheet"],
            headers=[str(item) for item in headers] if isinstance(headers, list) else [],
            row_count=int(row["row_count"]),
            truncated=bool(row["truncated"]),
            notes=list(json.loads(row["notes"] or "[]")),
        )
