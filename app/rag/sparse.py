"""Lexical (BM25) index — the keyword half of hybrid retrieval (PRD 14).

Stored per ``(organization_id, knowledge_base_id)`` pair so a lexical query can
never see another tenant's documents, exactly like the vector side. Documents are
JSON files on disk: small, inspectable, no extra service to run.

Versi 2 (indeks lama = versi 1):

* **Inverted index sendiri, bukan rank_bm25.** BM25Plus dulu memberi skor positif ke SEMUA
  potongan - termasuk yang tidak memuat satu pun kata kueri - sehingga daftar hasil selalu
  penuh potongan tak relevan. Sekarang hanya potongan yang memuat minimal satu istilah kueri
  yang dinilai, dan biayanya sebanding jumlah kecocokan, bukan jumlah seluruh potongan.
* **Analisis teks bahasa Indonesia** (``textnorm``): kata tugas dibuang, bentuk dasar ikut.
* **Nama dokumen + bagian ikut terindeks**, dan ``is_summary`` ikut tersimpan (dulu hilang
  setiap restart sehingga ringkasan ikut bersaing di pencarian biasa).

Indeks versi 1 tetap terbaca (token lamanya dipakai setelah kata tugas dibuang) dan dibangun
ulang di latar belakang dari isi Qdrant oleh :func:`migrate_legacy_scopes`.
"""

from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from app.core.config import Settings
from app.core.logging import get_logger
from app.rag.constants import SUMMARY_CHUNK_ID
from app.rag.textnorm import STOPWORDS, keywords, sparse_text, term_forms, unique

logger = get_logger(__name__)

INDEX_VERSION = 2
K1 = 1.2
B = 0.75

# (chunk_id, document_id, text) atau (chunk_id, document_id, text, is_summary)
Row = Tuple


@dataclass
class SparseEntry:
    chunk_id: str
    document_id: str
    # Versi 2: kata bermakna (permukaan). Bentuk dasarnya dihitung saat indeks dibangun, jadi
    # perbaikan stemming berlaku tanpa membangun ulang berkas.
    tokens: List[str]
    # Potongan ringkasan dokumen: ikut disimpan supaya bisa dicari saat pertanyaannya memang
    # minta ringkasan, tetapi tidak ikut bersaing pada pencarian biasa.
    is_summary: bool = False


@dataclass
class _Scope:
    entries: List[SparseEntry] = field(default_factory=list)
    legacy: bool = False
    generation: int = 0
    dirty: bool = True
    postings: Dict[str, Dict[int, int]] = field(default_factory=dict)
    lengths: List[int] = field(default_factory=list)
    avgdl: float = 1.0


def tokenize(text: str) -> List[str]:
    """Kata bermakna yang disimpan per potongan (bentuk dasarnya dihitung saat indeks dibangun)."""
    return keywords(text)


def _entry_terms(entry: SparseEntry) -> List[str]:
    out: List[str] = []
    for token in entry.tokens:
        out.extend(term_forms(token))
    return out


class SparseIndex:
    """Persisted BM25 index, partitioned by tenant scope."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._root = Path(settings.sparse_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._scopes: Dict[str, _Scope] = {}

    # ------------------------------------------------------------------ #
    @staticmethod
    def scope_key(organization_id: str, knowledge_base_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{organization_id}__{knowledge_base_id}")
        return safe

    def _path(self, scope: str) -> Path:
        return self._root / f"{scope}.json"

    def _load(self, path: Path) -> _Scope:
        loaded = _Scope()
        if not path.exists():
            return loaded
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("sparse index %s unreadable (%s); starting empty", path.name, exc)
            return loaded
        version = int(payload.get("version") or 1)
        entries: List[SparseEntry] = []
        for item in payload.get("entries", []):
            chunk_id = str(item.get("chunk_id") or "")
            tokens = list(item.get("tokens") or [])
            if version < INDEX_VERSION:
                # Token lama memuat kata tugas dan belum dipangkas; yang bermakna tetap dipakai
                # (bentuk dasarnya dihitung saat membangun posting) sampai dibangun ulang.
                tokens = [token for token in tokens if token not in STOPWORDS]
            entries.append(
                SparseEntry(
                    chunk_id=chunk_id,
                    document_id=str(item.get("document_id") or ""),
                    tokens=tokens,
                    is_summary=bool(item.get("is_summary")) or chunk_id == SUMMARY_CHUNK_ID,
                )
            )
        loaded.entries = entries
        loaded.legacy = version < INDEX_VERSION
        return loaded

    def _scope(self, organization_id: str, knowledge_base_id: str) -> _Scope:
        scope = self.scope_key(organization_id, knowledge_base_id)
        with self._lock:
            if scope not in self._scopes:
                self._scopes[scope] = self._load(self._path(scope))
            return self._scopes[scope]

    def _persist(self, organization_id: str, knowledge_base_id: str, scope_obj: _Scope) -> None:
        scope = self.scope_key(organization_id, knowledge_base_id)
        payload = {
            "version": 1 if scope_obj.legacy else INDEX_VERSION,
            "organization_id": organization_id,
            "knowledge_base_id": knowledge_base_id,
            "entries": [
                dict(chunk_id=e.chunk_id, document_id=e.document_id, tokens=e.tokens, is_summary=e.is_summary)
                for e in scope_obj.entries
            ],
        }
        tmp = self._path(scope).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path(scope))

    def _touch(self, scope_obj: _Scope) -> None:
        scope_obj.dirty = True
        scope_obj.generation += 1

    def _ensure_postings(self, scope_obj: _Scope) -> None:
        if not scope_obj.dirty:
            return
        postings: Dict[str, Dict[int, int]] = {}
        lengths: List[int] = []
        for index, entry in enumerate(scope_obj.entries):
            terms = _entry_terms(entry)
            lengths.append(len(terms))
            for term in terms:
                bucket = postings.setdefault(term, {})
                bucket[index] = bucket.get(index, 0) + 1
        scope_obj.postings = postings
        scope_obj.lengths = lengths
        scope_obj.avgdl = (sum(lengths) / len(lengths)) if lengths else 1.0
        scope_obj.dirty = False

    @staticmethod
    def _idf(total: int, frequency: int) -> float:
        return math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))

    # ------------------------------------------------------------------ #
    def upsert(
        self,
        organization_id: str,
        knowledge_base_id: str,
        items: Sequence[Row],
    ) -> int:
        """``items`` = (chunk_id, document_id, text[, is_summary]). Replaces those chunks.

        Identity is the *(document_id, chunk_id)* pair: ``chunk_0001`` exists in every
        document, so matching on the bare chunk id would make a second document's
        indexing silently delete the first document's lexical entries.
        """
        added = 0
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            incoming = {(item[1], item[0]) for item in items}
            scope_obj.entries = [
                entry for entry in scope_obj.entries if (entry.document_id, entry.chunk_id) not in incoming
            ]
            for item in items:
                chunk_id, document_id, text = item[0], item[1], item[2]
                summary = bool(item[3]) if len(item) > 3 else chunk_id == SUMMARY_CHUNK_ID
                scope_obj.entries.append(
                    SparseEntry(chunk_id=chunk_id, document_id=document_id, tokens=keywords(text), is_summary=summary)
                )
                added += 1
            self._touch(scope_obj)
            self._persist(organization_id, knowledge_base_id, scope_obj)
        return added

    def remove_document(self, organization_id: str, knowledge_base_id: str, document_id: str) -> int:
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            before = len(scope_obj.entries)
            scope_obj.entries = [entry for entry in scope_obj.entries if entry.document_id != document_id]
            removed = before - len(scope_obj.entries)
            if removed:
                self._touch(scope_obj)
                self._persist(organization_id, knowledge_base_id, scope_obj)
        return removed

    def drop_scope(self, organization_id: str, knowledge_base_id: str) -> None:
        scope = self.scope_key(organization_id, knowledge_base_id)
        with self._lock:
            self._scopes.pop(scope, None)
            path = self._path(scope)
            if path.exists():
                path.unlink()

    def search(
        self,
        query: str,
        organization_id: str,
        knowledge_base_id: str,
        top_k: int = 30,
        document_ids: Optional[Sequence[str]] = None,
        include_summary: bool = False,
    ) -> List[Tuple[str, float]]:
        """Return ``[(f"{document_id}::{chunk_id}", normalised_score)]``.

        The key is composite because ``chunk_0001`` repeats in every document; a bare
        chunk id would make two documents indistinguishable for the fusion stage.

        Hanya potongan yang memuat minimal satu istilah kueri yang dikembalikan. ``document_ids``
        hanya MENYARING kandidat; statistik IDF tetap seluruh scope.
        """
        terms = unique(term for token in keywords(query) for term in term_forms(token))
        if not terms:
            return []
        scope_obj = self._scope(organization_id, knowledge_base_id)
        wanted = {str(item) for item in (document_ids or []) if item} or None
        with self._lock:
            if not scope_obj.entries:
                return []
            self._ensure_postings(scope_obj)
            total = len(scope_obj.entries)
            scores: Dict[int, float] = {}
            for term in terms:
                bucket = scope_obj.postings.get(term)
                if not bucket:
                    continue
                idf = self._idf(total, len(bucket))
                for index, frequency in bucket.items():
                    entry = scope_obj.entries[index]
                    if wanted is not None and entry.document_id not in wanted:
                        continue
                    if entry.is_summary and not include_summary:
                        continue
                    norm = K1 * (1 - B + B * scope_obj.lengths[index] / scope_obj.avgdl)
                    scores[index] = scores.get(index, 0.0) + idf * frequency * (K1 + 1) / (frequency + norm)
            ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
            keys = [
                (f"{scope_obj.entries[index].document_id}::{scope_obj.entries[index].chunk_id}", score)
                for index, score in ranked
            ]
        if not keys:
            return []
        best = keys[0][1] or 1.0
        return [(key, score / best) for key, score in keys]

    def idf_function(self, organization_id: str, knowledge_base_id: str) -> Callable[[str], float]:
        """IDF tingkat korpus (seluruh scope) untuk reranker: kata yang langka di SELURUH
        knowledge lebih menentukan - jauh lebih stabil daripada IDF dari segelintir kandidat."""
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            self._ensure_postings(scope_obj)
            total = max(1, len(scope_obj.entries))
            frequencies = {term: len(bucket) for term, bucket in scope_obj.postings.items()}

        def idf(term: str) -> float:
            return self._idf(total, frequencies.get(term, 0))

        return idf

    def document_ids(self, organization_id: str, knowledge_base_id: str) -> List[str]:
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            return sorted({entry.document_id for entry in scope_obj.entries})

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {scope: len(scope_obj.entries) for scope, scope_obj in self._scopes.items()}

    # ------------------------------------------------------------------ #
    # Pembangunan ulang indeks lama
    # ------------------------------------------------------------------ #
    def legacy_scopes(self) -> List[Tuple[str, str]]:
        """``[(organization_id, knowledge_base_id)]`` yang berkasnya masih versi lama."""
        found: List[Tuple[str, str]] = []
        for path in sorted(self._root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if int(payload.get("version") or 1) >= INDEX_VERSION:
                continue
            organization_id = str(payload.get("organization_id") or "")
            knowledge_base_id = str(payload.get("knowledge_base_id") or "")
            if organization_id and knowledge_base_id:
                found.append((organization_id, knowledge_base_id))
        return found

    def generation(self, organization_id: str, knowledge_base_id: str) -> int:
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            return scope_obj.generation

    def rebuild_scope(
        self,
        organization_id: str,
        knowledge_base_id: str,
        rows: Iterable[Row],
        *,
        expected_generation: Optional[int] = None,
    ) -> bool:
        """Ganti seluruh isi scope. Ditolak (False) bila scope berubah sejak ``expected_generation``
        - pengindeksan yang berjalan bersamaan tidak boleh tertimpa data yang lebih lama."""
        entries = [
            SparseEntry(
                chunk_id=row[0],
                document_id=row[1],
                tokens=keywords(row[2]),
                is_summary=bool(row[3]) if len(row) > 3 else row[0] == SUMMARY_CHUNK_ID,
            )
            for row in rows
        ]
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            if expected_generation is not None and scope_obj.generation != expected_generation:
                return False
            scope_obj.entries = entries
            scope_obj.legacy = False
            self._touch(scope_obj)
            self._persist(organization_id, knowledge_base_id, scope_obj)
        return True


def qdrant_rows_loader(settings: Settings) -> Callable[[str, str, Sequence[str]], List[Row]]:
    """Baca ulang isi potongan dari Qdrant (sumber kebenaran) untuk membangun ulang BM25."""
    from app.qdrant import repository

    def load(organization_id: str, knowledge_base_id: str, document_ids: Sequence[str]) -> List[Row]:
        rows: List[Row] = []
        for document_id in document_ids:
            for payload in repository.list_document_chunks(
                settings,
                organization_id=organization_id,
                document_id=document_id,
                knowledge_base_id=knowledge_base_id,
                include_summary=True,
            ):
                chunk_id = str(payload.get("chunk_id") or "")
                if not chunk_id:
                    continue
                rows.append(
                    (
                        chunk_id,
                        document_id,
                        sparse_text(
                            str(payload.get("document_name") or ""),
                            str(payload.get("section") or ""),
                            str(payload.get("content") or ""),
                        ),
                        bool(payload.get("is_summary")) or chunk_id == SUMMARY_CHUNK_ID,
                    )
                )
        return rows

    return load


def migrate_legacy_scopes(
    index: SparseIndex,
    loader: Callable[[str, str, Sequence[str]], List[Row]],
    *,
    attempts: int = 3,
) -> Dict[str, int]:
    """Bangun ulang setiap scope versi lama dari Qdrant. Aman dijalankan berulang.

    Bila isi Qdrant untuk scope itu kosong padahal indeks lama berisi, scope dibiarkan (lebih
    baik indeks lama daripada indeks kosong). Hasil: {scope: jumlah potongan} yang dibangun.
    """
    done: Dict[str, int] = {}
    for organization_id, knowledge_base_id in index.legacy_scopes():
        scope = index.scope_key(organization_id, knowledge_base_id)
        for _ in range(attempts):
            generation = index.generation(organization_id, knowledge_base_id)
            document_ids = index.document_ids(organization_id, knowledge_base_id)
            try:
                rows = loader(organization_id, knowledge_base_id, document_ids)
            except Exception as exc:  # noqa: BLE001
                logger.warning("bangun ulang BM25 %s gagal membaca Qdrant: %s", scope, exc)
                break
            if document_ids and not rows:
                logger.warning("bangun ulang BM25 %s dilewati: Qdrant tidak mengembalikan potongan", scope)
                break
            if index.rebuild_scope(organization_id, knowledge_base_id, rows, expected_generation=generation):
                done[scope] = len(rows)
                logger.info("indeks BM25 %s dibangun ulang (versi %d): %d potongan", scope, INDEX_VERSION, len(rows))
                break
    return done
