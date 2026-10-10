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
# Scope besar tidak ditulis ulang utuh di setiap unggahan (KB 30 ribu potongan = berkas 50 MB,
# ~0,6 dtk per dokumen, dan pencarian ikut menunggu). Perubahannya ditambahkan ke jurnal kecil,
# dan berkas utama dipadatkan setiap JOURNAL_COMPACT_OPS perubahan.
JOURNAL_MIN_ENTRIES = 5000
JOURNAL_COMPACT_OPS = 300
JOURNAL_COMPACT_BYTES = 16 * 1024 * 1024
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
    # ID entri stabil -> entri. ID tidak pernah dipakai ulang, jadi posting bisa diperbarui
    # sebagian (tambah/hapus satu dokumen) tanpa membangun ulang seluruh indeks.
    entries: Dict[int, SparseEntry] = field(default_factory=dict)
    keys: Dict[Tuple[str, str], int] = field(default_factory=dict)
    next_id: int = 0
    legacy: bool = False
    generation: int = 0
    built: bool = False
    postings: Dict[str, Dict[int, int]] = field(default_factory=dict)
    lengths: Dict[int, int] = field(default_factory=dict)
    total_length: int = 0
    organization_id: str = ""
    knowledge_base_id: str = ""
    # Jumlah perubahan di jurnal sejak berkas utama terakhir ditulis.
    journal_ops: int = 0
    journal_bytes: int = 0

    @property
    def avgdl(self) -> float:
        return (self.total_length / len(self.lengths)) if self.lengths else 1.0


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
        # Penomoran generasi GLOBAL: scope yang dihapus lalu terbentuk lagi tidak pernah
        # mendapat nomor yang sama, jadi pembangunan ulang yang tertinggal tidak bisa menimpanya.
        self._generations = 0
        # Ringkasan berkas per scope (mtime -> organisasi, KB, jumlah) untuk daftar KB.
        self._file_meta: Dict[str, Tuple[float, str, str, int, int]] = {}

    # ------------------------------------------------------------------ #
    @staticmethod
    def scope_key(organization_id: str, knowledge_base_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{organization_id}__{knowledge_base_id}")
        return safe

    def _path(self, scope: str) -> Path:
        return self._root / f"{scope}.json"

    def _journal(self, scope: str) -> Path:
        return self._root / f"{scope}.journal"

    @staticmethod
    def _entry_dict(entry: SparseEntry) -> Dict[str, object]:
        return dict(chunk_id=entry.chunk_id, document_id=entry.document_id, tokens=entry.tokens,
                    is_summary=entry.is_summary)

    def _replay(self, scope_obj: _Scope, path: Path) -> None:
        """Terapkan jurnal di atas berkas utama. Baris terakhir yang terpotong (mati saat menulis)
        diabaikan - perubahan sebelumnya tetap berlaku."""
        if not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("jurnal BM25 %s tidak terbaca: %s", path.name, exc)
            return
        for line in lines:
            try:
                op = json.loads(line)
            except ValueError:
                break
            if op.get("op") == "upsert":
                for item in op.get("entries", []):
                    entry = SparseEntry(
                        chunk_id=str(item.get("chunk_id") or ""),
                        document_id=str(item.get("document_id") or ""),
                        tokens=list(item.get("tokens") or []),
                        is_summary=bool(item.get("is_summary")),
                    )
                    existing = scope_obj.keys.get((entry.document_id, entry.chunk_id))
                    if existing is not None:
                        self._remove_entry(scope_obj, existing)
                    self._add_entry(scope_obj, entry)
            elif op.get("op") == "remove":
                document_id = str(op.get("document_id") or "")
                for entry_id in [i for i, e in scope_obj.entries.items() if e.document_id == document_id]:
                    self._remove_entry(scope_obj, entry_id)
            scope_obj.journal_ops += 1
            scope_obj.journal_bytes += len(line) + 1

    def _record(self, organization_id: str, knowledge_base_id: str, scope_obj: _Scope, op: Dict[str, object]) -> None:
        """Simpan satu perubahan: scope kecil ditulis utuh, scope besar lewat jurnal."""
        scope = self.scope_key(organization_id, knowledge_base_id)
        if (
            len(scope_obj.entries) < JOURNAL_MIN_ENTRIES
            or scope_obj.legacy
            or scope_obj.journal_ops + 1 >= JOURNAL_COMPACT_OPS
            or scope_obj.journal_bytes >= JOURNAL_COMPACT_BYTES
            or not self._path(scope).exists()
        ):
            self._persist(organization_id, knowledge_base_id, scope_obj)
            return
        line = json.dumps(op, ensure_ascii=False) + "\n"
        with self._journal(scope).open("a", encoding="utf-8") as handle:
            handle.write(line)
        scope_obj.journal_ops += 1
        scope_obj.journal_bytes += len(line)

    def _next_generation(self) -> int:
        self._generations += 1
        return self._generations

    @staticmethod
    def _add_entry(scope_obj: _Scope, entry: SparseEntry) -> None:
        entry_id = scope_obj.next_id
        scope_obj.next_id += 1
        scope_obj.entries[entry_id] = entry
        scope_obj.keys[(entry.document_id, entry.chunk_id)] = entry_id
        if scope_obj.built:
            terms = _entry_terms(entry)
            scope_obj.lengths[entry_id] = len(terms)
            scope_obj.total_length += len(terms)
            for term in terms:
                bucket = scope_obj.postings.setdefault(term, {})
                bucket[entry_id] = bucket.get(entry_id, 0) + 1

    @staticmethod
    def _remove_entry(scope_obj: _Scope, entry_id: int) -> None:
        entry = scope_obj.entries.pop(entry_id, None)
        if entry is None:
            return
        scope_obj.keys.pop((entry.document_id, entry.chunk_id), None)
        if scope_obj.built:
            for term in set(_entry_terms(entry)):
                bucket = scope_obj.postings.get(term)
                if bucket is not None:
                    bucket.pop(entry_id, None)
                    if not bucket:
                        del scope_obj.postings[term]
            scope_obj.total_length -= scope_obj.lengths.pop(entry_id, 0)

    def _load(self, path: Path) -> _Scope:
        loaded = _Scope(generation=self._next_generation())
        if not path.exists():
            return loaded
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("sparse index %s unreadable (%s); starting empty", path.name, exc)
            return loaded
        version = int(payload.get("version") or 1)
        for item in payload.get("entries", []):
            chunk_id = str(item.get("chunk_id") or "")
            tokens = list(item.get("tokens") or [])
            if version < INDEX_VERSION:
                # Token lama memuat kata tugas dan belum dipangkas; yang bermakna tetap dipakai
                # (bentuk dasarnya dihitung saat membangun posting) sampai dibangun ulang.
                tokens = [token for token in tokens if token not in STOPWORDS]
            self._add_entry(
                loaded,
                SparseEntry(
                    chunk_id=chunk_id,
                    document_id=str(item.get("document_id") or ""),
                    tokens=tokens,
                    is_summary=bool(item.get("is_summary")) or chunk_id == SUMMARY_CHUNK_ID,
                ),
            )
        loaded.legacy = version < INDEX_VERSION
        loaded.organization_id = str(payload.get("organization_id") or "")
        loaded.knowledge_base_id = str(payload.get("knowledge_base_id") or "")
        self._replay(loaded, path.with_suffix(".journal"))
        return loaded

    def _scope(self, organization_id: str, knowledge_base_id: str) -> _Scope:
        scope = self.scope_key(organization_id, knowledge_base_id)
        with self._lock:
            if scope not in self._scopes:
                loaded = self._load(self._path(scope))
                loaded.organization_id, loaded.knowledge_base_id = organization_id, knowledge_base_id
                self._scopes[scope] = loaded
            return self._scopes[scope]

    def _persist(self, organization_id: str, knowledge_base_id: str, scope_obj: _Scope) -> None:
        scope = self.scope_key(organization_id, knowledge_base_id)
        payload = {
            "version": 1 if scope_obj.legacy else INDEX_VERSION,
            "organization_id": organization_id,
            "knowledge_base_id": knowledge_base_id,
            "entries": [
                dict(chunk_id=e.chunk_id, document_id=e.document_id, tokens=e.tokens, is_summary=e.is_summary)
                for e in scope_obj.entries.values()
            ],
        }
        tmp = self._path(scope).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path(scope))
        journal = self._journal(scope)
        if journal.exists():
            journal.unlink()
        scope_obj.journal_ops = 0
        scope_obj.journal_bytes = 0

    def _changed(self, scope_obj: _Scope) -> None:
        scope_obj.generation = self._next_generation()

    def _ensure_postings(self, scope_obj: _Scope) -> None:
        """Bangun posting SEKALI (setelah dimuat); sesudahnya diperbarui per entri."""
        if scope_obj.built:
            return
        scope_obj.postings = {}
        scope_obj.lengths = {}
        scope_obj.total_length = 0
        for entry_id, entry in scope_obj.entries.items():
            terms = _entry_terms(entry)
            scope_obj.lengths[entry_id] = len(terms)
            scope_obj.total_length += len(terms)
            for term in terms:
                bucket = scope_obj.postings.setdefault(term, {})
                bucket[entry_id] = bucket.get(entry_id, 0) + 1
        scope_obj.built = True

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
        prepared = [
            SparseEntry(
                chunk_id=item[0],
                document_id=item[1],
                tokens=keywords(item[2]),
                is_summary=bool(item[3]) if len(item) > 3 else item[0] == SUMMARY_CHUNK_ID,
            )
            for item in items
        ]
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            for entry in prepared:
                existing = scope_obj.keys.get((entry.document_id, entry.chunk_id))
                if existing is not None:
                    self._remove_entry(scope_obj, existing)
                self._add_entry(scope_obj, entry)
            self._changed(scope_obj)
            self._record(organization_id, knowledge_base_id, scope_obj,
                         {"op": "upsert", "entries": [self._entry_dict(entry) for entry in prepared]})
        return len(prepared)

    def remove_document(self, organization_id: str, knowledge_base_id: str, document_id: str) -> int:
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            ids = [entry_id for entry_id, entry in scope_obj.entries.items() if entry.document_id == document_id]
            for entry_id in ids:
                self._remove_entry(scope_obj, entry_id)
            if ids:
                self._changed(scope_obj)
                self._record(organization_id, knowledge_base_id, scope_obj,
                             {"op": "remove", "document_id": document_id})
        return len(ids)

    def drop_scope(self, organization_id: str, knowledge_base_id: str) -> None:
        scope = self.scope_key(organization_id, knowledge_base_id)
        with self._lock:
            # Scope kosong baru dengan generasi baru: pembangunan ulang yang masih berjalan untuk
            # scope ini akan ditolak (generasinya tidak cocok) dan tidak menghidupkannya lagi.
            self._scopes[scope] = _Scope(
                generation=self._next_generation(), built=True,
                organization_id=organization_id, knowledge_base_id=knowledge_base_id,
            )
            self._file_meta.pop(scope, None)
            for path in (self._path(scope), self._journal(scope)):
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
            avgdl = scope_obj.avgdl or 1.0
            scores: Dict[int, float] = {}
            for term in terms:
                bucket = scope_obj.postings.get(term)
                if not bucket:
                    continue
                idf = self._idf(total, len(bucket))
                for entry_id, frequency in bucket.items():
                    entry = scope_obj.entries[entry_id]
                    if wanted is not None and entry.document_id not in wanted:
                        continue
                    if entry.is_summary and not include_summary:
                        continue
                    norm = K1 * (1 - B + B * scope_obj.lengths.get(entry_id, 0) / avgdl)
                    scores[entry_id] = scores.get(entry_id, 0.0) + idf * frequency * (K1 + 1) / (frequency + norm)
            ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
            keys = [
                (f"{scope_obj.entries[entry_id].document_id}::{scope_obj.entries[entry_id].chunk_id}", score)
                for entry_id, score in ranked
            ]
        if not keys:
            return []
        best = keys[0][1] or 1.0
        return [(key, score / best) for key, score in keys]

    def idf_function(self, organization_id: str, knowledge_base_id: str) -> Callable[[str], float]:
        """IDF tingkat korpus (seluruh scope) untuk reranker: kata yang langka di SELURUH
        knowledge lebih menentukan - jauh lebih stabil daripada IDF dari segelintir kandidat.

        Tidak menyalin kosakata (mahal saat knowledge besar): frekuensi dibaca saat dibutuhkan."""
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            self._ensure_postings(scope_obj)
            total = max(1, len(scope_obj.entries))
            postings = scope_obj.postings

        def idf(term: str) -> float:
            return self._idf(total, len(postings.get(term) or ()))

        return idf

    def document_ids(self, organization_id: str, knowledge_base_id: str) -> List[str]:
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            return sorted({entry.document_id for entry in scope_obj.entries.values()})

    def document_chunk_counts(self, organization_id: str, knowledge_base_id: str) -> Dict[str, int]:
        """``{document_id: jumlah potongan isi}`` di satu scope."""
        scope_obj = self._scope(organization_id, knowledge_base_id)
        counts: Dict[str, int] = {}
        with self._lock:
            for entry in scope_obj.entries.values():
                if not entry.is_summary:
                    counts[entry.document_id] = counts.get(entry.document_id, 0) + 1
        return counts

    def knowledge_bases(self, organization_id: str) -> Dict[str, Dict[str, int]]:
        """``{knowledge_base_id: {"documents", "chunks"}}`` milik satu organisasi (isi, bukan ringkasan).

        Scope yang sudah dimuat dihitung dari memori; berkas lain hanya dibaca bila berubah sejak
        terakhir dibaca (cache per mtime) - daftar KB tidak mem-parse ulang semua berkas.
        """
        prefix = self.scope_key(organization_id, "")
        found: Dict[str, Dict[str, int]] = {}
        with self._lock:
            in_memory = {scope: obj for scope, obj in self._scopes.items() if obj.organization_id == organization_id}
            for scope_obj in in_memory.values():
                content = [entry.document_id for entry in scope_obj.entries.values() if not entry.is_summary]
                if content and scope_obj.knowledge_base_id:
                    found[scope_obj.knowledge_base_id] = {"documents": len(set(content)), "chunks": len(content)}
        for path in sorted(self._root.glob("*.json")):
            scope = path.stem
            if not scope.startswith(prefix) or scope in in_memory:
                continue
            if self._journal(scope).exists():
                # Berkas utama belum memuat perubahan terbaru: hitung dari scope yang dimuat.
                loaded = self._load(path)
                content = [entry.document_id for entry in loaded.entries.values() if not entry.is_summary]
                if loaded.organization_id == organization_id and loaded.knowledge_base_id and content:
                    found[loaded.knowledge_base_id] = {"documents": len(set(content)), "chunks": len(content)}
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            with self._lock:
                cached = self._file_meta.get(scope)
            if cached is None or cached[0] != mtime:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                content = [
                    str(item.get("document_id") or "")
                    for item in payload.get("entries", [])
                    if not (bool(item.get("is_summary")) or item.get("chunk_id") == SUMMARY_CHUNK_ID)
                ]
                cached = (
                    mtime,
                    str(payload.get("organization_id") or ""),
                    str(payload.get("knowledge_base_id") or ""),
                    len(set(content)),
                    len(content),
                )
                with self._lock:
                    self._file_meta[scope] = cached
            _mtime, owner, knowledge_base_id, documents, chunks = cached
            if owner == organization_id and knowledge_base_id:
                found[knowledge_base_id] = {"documents": documents, "chunks": chunks}
        return found

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {scope: len(scope_obj.entries) for scope, scope_obj in self._scopes.items()}

    def warm(self) -> int:
        """Muat semua scope dan bangun postingnya di LUAR kunci global (saat layanan menyala).

        Tanpa ini, pertanyaan pertama ke KB besar setelah restart/deploy menunggu indeksnya
        dimuat dan dibangun (~5 dtk untuk 30 ribu potongan), dan selama itu pencarian KB lain
        ikut tertahan oleh kunci yang sama.
        """
        warmed = 0
        for path in sorted(self._root.glob("*.json")):
            scope = path.stem
            with self._lock:
                if scope in self._scopes:
                    continue
            loaded = self._load(path)
            if not loaded.organization_id or not loaded.knowledge_base_id or loaded.legacy:
                continue
            self._ensure_postings(loaded)
            with self._lock:
                if scope in self._scopes:
                    continue
                loaded.generation = self._next_generation()
                self._scopes[scope] = loaded
                warmed += 1
        return warmed

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
        fresh = _Scope()
        for row in rows:
            self._add_entry(
                fresh,
                SparseEntry(
                    chunk_id=row[0],
                    document_id=row[1],
                    tokens=keywords(row[2]),
                    is_summary=bool(row[3]) if len(row) > 3 else row[0] == SUMMARY_CHUNK_ID,
                ),
            )
        scope = self.scope_key(organization_id, knowledge_base_id)
        with self._lock:
            current = self._scope(organization_id, knowledge_base_id)
            if expected_generation is not None and current.generation != expected_generation:
                return False
            fresh.generation = self._next_generation()
            fresh.organization_id, fresh.knowledge_base_id = organization_id, knowledge_base_id
            self._scopes[scope] = fresh
            self._persist(organization_id, knowledge_base_id, fresh)
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
